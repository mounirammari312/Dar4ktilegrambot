import os
import datetime
import urllib.request
import ssl
import json
import asyncio
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from mistralai import Mistral
from motor.motor_asyncio import AsyncIOMotorClient

MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "LFFnmVcXCEbVT9eFYMkKqT9tQnwNkxCt")
MONGODB_URI = os.environ.get("MONGODB_URI")
TG_API_ID = 3302130
TG_API_HASH = "3a2b0faf91e914beb2824b052bd97e1c"
TARGET_ACCOUNT_ID = "3302130"

IPTV_KEYWORDS_MATRIX = [
    "iptv", "m3u", "xtream", "xstream", "m3u8", "playlist",
    "اشتراك", "اشترك", "سيرفر", "سرفر", "سيرفرات", "سرفرات",
    "subscription", "sub ", "server", "servers",
    "abonnement", "abonner", "serveur", "serveurs",
    "suscripcion", "suscripción", "servidor", "servidores",
    "abo ", "server", "servidor"
]

class AhoCorasickAutomaton:
    __slots__ = ['trie', 'fail', 'output']
    def __init__(self):
        self.trie = [{}]
        self.fail = [0]
        self.output = [False]
    def add_word(self, word):
        curr = 0
        for char in word:
            if char not in self.trie[curr]:
                self.trie[curr][char] = len(self.trie)
                self.trie.append({})
                self.fail.append(0)
                self.output.append(False)
            curr = self.trie[curr][char]
        self.output[curr] = True
    def build(self):
        queue = []
        for char, node in self.trie[0].items():
            self.fail[node] = 0
            queue.append(node)
        while queue:
            curr = queue.pop(0)
            for char, node in self.trie[curr].items():
                fail_node = self.fail[curr]
                while fail_node > 0 and char not in self.trie[fail_node]:
                    fail_node = self.fail[fail_node]
                if char in self.trie[fail_node]:
                    fail_node = self.trie[fail_node][char]
                self.fail[node] = fail_node
                if self.output[fail_node]:
                    self.output[node] = True
                queue.append(node)
    def match(self, text):
        curr = 0
        for char in text:
            while curr > 0 and char not in self.trie[curr]:
                curr = self.fail[curr]
            if char in self.trie[curr]:
                curr = self.trie[curr][char]
            if self.output[curr]:
                return True
        return False

class PIDDelayController:
    __slots__ = ['kp', 'ki', 'kd', 'base_delay', 'integral', 'prev_error', 'last_time']
    def __init__(self, kp=0.1, ki=0.01, kd=0.05, base_delay=3.0):
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.base_delay = base_delay
        self.integral = 0.0
        self.prev_error = 0.0
        self.last_time = datetime.datetime.now()
    def process_and_get_delay(self, current_rate):
        error = current_rate - 1.0
        now = datetime.datetime.now()
        dt = (now - self.last_time).total_seconds()
        if dt <= 0: dt = 0.1
        self.integral += error * dt
        derivative = (error - self.prev_error) / dt
        output = (self.kp * error) + (self.ki * self.integral) + (self.kd * derivative)
        self.prev_error = error
        self.last_time = now
        return max(1.5, min(12.0, self.base_delay + output))

class LSHDuplicateFilter:
    __slots__ = ['cache', 'max_size']
    def __init__(self, max_size=500):
        self.cache = {}
        self.max_size = max_size
    def is_spam_or_duplicate(self, text):
        clean = "".join([c for c in text if c.isalnum()]).lower()
        if len(clean) < 6: return True
        shingles = frozenset(clean[i:i+4] for i in range(len(clean)-3))
        fingerprint = hash(shingles)
        if fingerprint in self.cache:
            return True
        if len(self.cache) >= self.max_size:
            del self.cache[next(iter(self.cache))]
        self.cache[fingerprint] = True
        return False

automaton = AhoCorasickAutomaton()
for kw in IPTV_KEYWORDS_MATRIX:
    automaton.add_word(kw)
automaton.build()

pid_controller = PIDDelayController()
spam_filter = LSHDuplicateFilter()
mistral_client = Mistral(api_key=MISTRAL_API_KEY)
message_timestamps = []
tg_client = None

async def handle_render_ping(reader, writer):
    try:
        await reader.read(1024)
        response = "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"
        writer.write(response.encode())
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

async def start_dummy_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = await asyncio.start_server(handle_render_ping, "0.0.0.0", port)
    print(f"📡 [Web Service Firewall] Dummy HTTP server listening on port {port}")

async def init_tg_client_from_mongodb():
    global tg_client
    if not MONGODB_URI:
        raise ValueError("CRITICAL_ERROR: MONGODB_URI environment variable is missing!")
    
    db_client = AsyncIOMotorClient(MONGODB_URI)
    db = db_client["darpro4k_db"]
    collection = db["darpro_sessions"]
    
    account_doc = await collection.find_one({"_id": TARGET_ACCOUNT_ID})
    if not account_doc or "string_session" not in account_doc:
        raise ValueError(f"CRITICAL_ERROR: Account document for ID {TARGET_ACCOUNT_ID} not found in MongoDB!")
    
    extracted_session = account_doc["string_session"].strip()
    tg_client = TelegramClient(StringSession(extracted_session), TG_API_ID, TG_API_HASH)

async def setup_event_handlers():
    @tg_client.on(events.NewMessage(incoming=True))
    async def live_message_handler(event):
        captured_text = event.message.message
        if not captured_text or not captured_text.strip():
            return

        try:
            sender = await event.get_sender()
            if not sender: return
            me = await tg_client.get_me()
            if sender.id == me.id: return
        except Exception:
            return

        if not automaton.match(captured_text.lower()):
            return

        if spam_filter.is_spam_or_duplicate(captured_text):
            return

        print(f"📩 [إشارة خوارزمية سحابية] اقتناص فرصة حية: '{captured_text[:40]}...'")

        classifier_instruction = (
            f"Analyze this chat message: '{captured_text}'. "
            "Does the sender express interest in buying an IPTV subscription, ask for IPTV server recommendations, "
            "complain about their current IPTV freezing/channels cutting, ask for free IPTV codes/M3U/Xtream, "
            "or ask how to setup/activate an IPTV app? "
            "Answer with exactly one word: 'YES' or 'NO'. Do not add any other words, punctuation, or explanations."
        )

        try:
            classifier_response = mistral_client.chat.complete(
                model="mistral-small-latest",
                messages=[{"role": "user", "content": classifier_instruction}]
            )
            
            decision = classifier_response.choices[0].message.content.strip().upper()
            
            if "NO" in decision:
                return
                
            print(f"🎯 [تأكيد النية] البواب الذكي أكد الهدف بنجاح.")

            now = datetime.datetime.now()
            message_timestamps.append(now)
            message_timestamps[:] = [t for t in message_timestamps if (now - t).total_seconds() < 60]
            current_rate = len(message_timestamps)
            
            calculated_delay = pid_controller.process_and_get_delay(current_rate)
            await asyncio.sleep(calculated_delay)

            agent_instruction = (
                f"The customer raw message is: '{captured_text}'\n\n"
                "STRICT OPERATIONAL PERSONA AND CONVERSION TUNNEL RULES:\n"
                "1. IDENTITY: You are 'Sami', a senior support specialist for 'Darpro4k'. Professional, warm, reassuring, and highly concise.\n"
                "2. CRITICAL LANGUAGE RULE: Analyze the language of the customer's message and reply 100% in the EXACT SAME LANGUAGE or dialect (English, Arabic, French, German, Portuguese, Spanish etc.). Never mix languages.\n"
                "3. VALUE-FIRST TEASER MESSAGING STRATEGY:\n"
                "   - Step 1: Start with a brief, friendly greeting as Sami from Darpro4k.\n"
                "   - Step 2: Inform them immediately that if they are looking for servers, free IPTV codes, M3U playlists, or Xtream access, our official website provides a completely FREE M3U & Xtream server generator tool they can use right now with zero sign-ups: https://m3u-generator-lovat.vercel.app\n"
                "   - Step 3: Pitch premium options smoothly. State that for users who demand absolute premium stability, 4K high-fidelity quality, and zero freezing during major live football matches or movies, we offer premium subscriptions at highly competitive prices starting at just $7 for a full month.\n"
                "   - Step 4: Mention that a 24-hour free trial is available so they can test this premium power themselves.\n"
                "   - Step 5: Direct them for immediate activation support or inquiries to contact the General Administration via WhatsApp at this exact link: https://wa.link/ysruwg\n"
                "   - STRICT FORBIDDEN RULE: Do NOT include any payment info, do NOT mention the PayPal email, and do NOT mention the rules about not writing IPTV in PayPal notes. Keep this first message purely focused on the free generator, 4K quality value, $7 pricing, and WhatsApp link.\n\n"
                "OUTPUT TARGET:\n"
                "Write a highly clean, beautifully structured, scannable text message to be sent directly to the customer's private DM. No notes, meta-text, or formatting explanations. Speak directly to the client as Sami."
            )

            agent_response = mistral_client.chat.complete(
                model="mistral-large-latest",
                messages=[{"role": "user", "content": agent_instruction}]
            )

            private_msg = agent_response.choices[0].message.content

            if not private_msg or not private_msg.strip():
                return

            try:
                await tg_client.send_message(event.sender_id, private_msg.strip())
                print("🔒 [نجاح قاطع] خرق الخاص سحابياً صامتاً وضخ المحفز الخوارزمي.")
                print("\n---------------------------------------------------\n")
            except Exception:
                pass

        except Exception:
            pass

async def main():
    print("=====================================================")
    print("🛰️ بدء تشغيل محرك Darpro4k السحابي المستقل (Web Service Mode)...")
    await asyncio.gather(
        start_dummy_web_server(),
        init_tg_client_from_mongodb()
    )
    await setup_event_handlers()
    await tg_client.start()
    print("⚡ [تأكيد أمني] الاتصال السحابي قائم حياً وبوابة الويب نشطة.")
    print("=====================================================\n")
    await tg_client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
