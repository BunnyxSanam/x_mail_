# --- START OF FILE main_modified.py ---

import logging
import smtplib
import ssl
from email.message import EmailMessage
import asyncio
import os
import time # Import time for potential rate limiting info

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters import Text, Command
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import (
    ParseMode, ReplyKeyboardRemove, User,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.utils import executor
from aiogram.utils.exceptions import MessageToDeleteNotFound, BotBlocked, MessageCantBeDeleted, MessageNotModified
from keep_alive import keep_alive
keep_alive()

# --- Configuration ---
# !!! IMPORTANT: SET YOUR BOT TOKEN IN REPLIT SECRETS !!!
# Key: BOT_TOKEN Value: Your_Telegram_Bot_Token
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger(__name__)

if not BOT_TOKEN:
    log.critical("FATAL ERROR: Bot token not found in Replit Secrets.")
    log.critical("Please set the 'BOT_TOKEN' secret in your Replit environment.")
    exit("Bot token not configured.")
else:
    log.info("Bot token loaded successfully from environment.")

# !!! REPLACE WITH YOUR TELEGRAM USER ID !!!
OWNER_ID = 7478752901
if not isinstance(OWNER_ID, int):
     log.warning("OWNER_ID is not set correctly. Please replace the placeholder with your numeric Telegram ID.")
     # You might want to exit here if owner functionality is critical
     # exit("Owner ID not configured.")

# --- Premium User Management ---
PREMIUM_USERS_FILE = "premium_users.txt"
premium_users = set()

# --- Constants ---
INTER_EMAIL_DELAY_SECONDS = 5.0 # Delay between sending emails from the SAME account (in seconds)
MAX_EMAILS_PER_RUN = 100 # Max emails per account per run (adjust as needed)
MAX_SENDER_ACCOUNTS = 10 # Limit number of sender accounts a user can add per run

# --- Bot Setup ---
storage = MemoryStorage() # Simple storage suitable for Replit
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(bot, storage=storage)

# --- Persistence Functions ---
def load_premium_users():
    """Loads premium user IDs from the file."""
    global premium_users
    premium_users = set()
    try:
        if os.path.exists(PREMIUM_USERS_FILE):
            with open(PREMIUM_USERS_FILE, 'r') as f:
                # Ensure only valid integer IDs are added
                loaded_ids = {int(line.strip()) for line in f if line.strip().isdigit()}
                premium_users.update(loaded_ids)
            log.info(f"Loaded {len(premium_users)} premium users from {PREMIUM_USERS_FILE}.")
        else:
            log.info(f"{PREMIUM_USERS_FILE} not found. Starting with empty premium list.")
            # Create the file if it doesn't exist to avoid errors on first save
            with open(PREMIUM_USERS_FILE, 'w') as f:
                pass
            log.info(f"Created empty {PREMIUM_USERS_FILE}.")
    except ValueError as e:
        log.error(f"Error converting user ID to int in {PREMIUM_USERS_FILE}: {e}. Check file content.")
    except Exception as e:
        log.error(f"Error loading premium users from {PREMIUM_USERS_FILE}: {e}")

def save_premium_users():
    """Saves the current set of premium user IDs to the file."""
    global premium_users
    try:
        with open(PREMIUM_USERS_FILE, 'w') as f:
            for user_id in sorted(list(premium_users)):
                f.write(f"{user_id}\n")
        log.info(f"Saved {len(premium_users)} premium users to {PREMIUM_USERS_FILE}.")
    except Exception as e:
        log.error(f"Error saving premium users to {PREMIUM_USERS_FILE}: {e}")

# --- Define States for FSM ---
class ReportForm(StatesGroup):
    # Sender Account Gathering
    waiting_for_email = State()         # Step 1a: Get sender email
    waiting_for_password = State()      # Step 1b: Get sender password
    ask_more_accounts = State()         # Step 1c: Ask if user wants to add more senders

    # SMTP and Target Details
    waiting_for_smtp_server = State()   # Step 2
    waiting_for_smtp_port = State()     # Step 3
    waiting_for_target_email = State()  # Step 4

    # Email Content and Count
    waiting_for_subject = State()       # Step 5
    waiting_for_body = State()          # Step 6
    waiting_for_count = State()         # Step 7

    # Final Confirmation
    waiting_for_confirmation = State()  # Waiting for final confirmation button

# --- Helper Functions ---
def is_allowed_user(user: User) -> bool:
    """Checks if the user is the owner or a premium user."""
    return user.id == OWNER_ID or user.id in premium_users

async def delete_message_safely(message: types.Message):
    """Attempts to delete a message, ignoring common safe errors."""
    if not message: return # Safety check
    try:
        await message.delete()
    except (MessageToDeleteNotFound, BotBlocked, MessageCantBeDeleted) as e:
        # These errors are common and usually safe to ignore silently
        log.debug(f"Minor error deleting message {message.message_id} in chat {message.chat.id}: {e}")
    except Exception as e:
        # Log other unexpected errors during deletion
        log.warning(f"Unexpected error deleting message {message.message_id} in chat {message.chat.id}: {e}")

# --- Email Sending Function (Handles Multiple Senders and Delay) ---
async def send_emails_async(user_data: dict, user_id: int, status_message: types.Message) -> tuple[bool, str]:
    """
    Sends emails using multiple sender accounts with delays, updating a status message.

    Args:
        user_data: Dictionary containing FSM state data.
        user_id: The Telegram ID of the user initiating the request.
        status_message: The message to edit with progress updates.

    Returns:
        A tuple (overall_success: bool, final_result_message: str)
    """
    sender_accounts = user_data.get('sender_accounts', [])
    smtp_server = user_data.get('smtp_server')
    smtp_port = user_data.get('smtp_port')
    target_email = user_data.get('target_email')
    subject = user_data.get('subject')
    body = user_data.get('body')
    count = user_data.get('count')

    # --- Initial Data Validation ---
    if not all([sender_accounts, smtp_server, smtp_port, target_email, subject, body, count]):
        log.error(f"User {user_id}: Missing data for sending email: {user_data}")
        return False, "❌ Internal error: Missing required data. Please start over using /report."

    try:
        port = int(smtp_port)
        count_int = int(count)
        if not (1 <= port <= 65535): raise ValueError("Invalid port range")
        if not (1 <= count_int <= MAX_EMAILS_PER_RUN) : raise ValueError(f"Count must be between 1 and {MAX_EMAILS_PER_RUN}")
    except ValueError as e:
        log.error(f"User {user_id}: Invalid port or count. Port='{smtp_port}', Count='{count}'. Error: {e}")
        return False, f"❌ Invalid input: {e}. Please check port (1-65535) and count (1-{MAX_EMAILS_PER_RUN})."

    total_senders = len(sender_accounts)
    log.info(f"User {user_id}: Starting email task. Target={target_email}, Count per Sender={count_int}, Senders={total_senders}, SMTP={smtp_server}:{port}, Delay={INTER_EMAIL_DELAY_SECONDS}s")

    overall_results_summary = []
    total_successfully_sent = 0
    total_attempted = count_int * total_senders
    context = ssl.create_default_context()

    start_time_total = time.monotonic()

    # --- Loop Through Each Sender Account ---
    for account_index, account in enumerate(sender_accounts):
        sender_email = account.get('email')
        sender_password = account.get('password') # Password will be used shortly
        sender_status_prefix = f"📧 Sender {account_index + 1}/{total_senders} (<code>{sender_email}</code>):"

        if not sender_email or not sender_password:
            log.warning(f"User {user_id}: Skipping invalid account data at index {account_index}")
            overall_results_summary.append(f"{sender_status_prefix} Skipped (incomplete credentials).")
            continue # Skip to the next sender account

        log.info(f"User {user_id}: Processing sender {account_index + 1}/{total_senders}: {sender_email}")
        await status_message.edit_text(
            f"⚙️ Processing Sender {account_index + 1}/{total_senders}...\n"
            f"Email: <code>{sender_email}</code>\n"
            f"Attempting connection to {smtp_server}:{port}..."
        )

        server = None
        sent_count_this_sender = 0
        errors_this_sender = []
        start_time_sender = time.monotonic()

        try:
            # --- Establish Connection ---
            log.debug(f"User {user_id}: Attempting connection to {smtp_server}:{port} for {sender_email}")
            if port == 465: # SSL Connection
                server = smtplib.SMTP_SSL(smtp_server, port, timeout=30, context=context)
                log.debug(f"User {user_id}: SMTP_SSL connection object created for {sender_email}.")
            else: # Standard Connection + STARTTLS
                server = smtplib.SMTP(smtp_server, port, timeout=30)
                log.debug(f"User {user_id}: SMTP connection object created for {sender_email}.")
                server.ehlo() # Identify client to server
                log.debug(f"User {user_id}: EHLO sent for {sender_email}.")
                server.starttls(context=context) # Upgrade to secure connection
                log.debug(f"User {user_id}: STARTTLS successful for {sender_email}.")
                server.ehlo() # Re-identify after STARTTLS
                log.debug(f"User {user_id}: EHLO sent again after STARTTLS for {sender_email}.")
            log.info(f"User {user_id}: Connection successful for {sender_email}.")

            # --- Login ---
            log.debug(f"User {user_id}: Attempting login for {sender_email}.")
            await status_message.edit_text(
                f"⚙️ Processing Sender {account_index + 1}/{total_senders}...\n"
                f"Email: <code>{sender_email}</code>\n"
                f"Authenticating..."
            )
            server.login(sender_email, sender_password)
            log.info(f"User {user_id}: Login successful for {sender_email}.")

            # --- Send Loop ---
            log.info(f"User {user_id}: Starting send loop for {sender_email}. Count: {count_int}")
            for i in range(count_int):
                current_email_num = i + 1
                progress_percent = ((account_index * count_int) + current_email_num) / total_attempted * 100

                await status_message.edit_text(
                     f"⏳ Sending... ({progress_percent:.1f}%)\n"
                     f"{sender_status_prefix}\n"
                     f"Sending email {current_email_num}/{count_int} to <code>{target_email}</code>...\n"
                     f"Delaying {INTER_EMAIL_DELAY_SECONDS}s..." # Show delay *before* sleeping
                 )
                # --- Apply Delay ---
                await asyncio.sleep(INTER_EMAIL_DELAY_SECONDS)

                try:
                    msg = EmailMessage()
                    msg['Subject'] = subject
                    msg['From'] = sender_email
                    msg['To'] = target_email
                    msg.set_content(body)

                    server.send_message(msg)
                    sent_count_this_sender += 1
                    total_successfully_sent += 1
                    log.info(f"User {user_id}: [{sender_email}] Email {current_email_num}/{count_int} sent to {target_email}.")

                    # Optional: Update status less frequently for very large counts to avoid hitting Telegram limits
                    # if current_email_num % 5 == 0 or current_email_num == count_int:
                    #     await status_message.edit_text(...) # Update with progress

                except smtplib.SMTPSenderRefused as e_loop:
                    error_msg = f"Sender address <code>{sender_email}</code> refused (maybe blocked or rate-limited?). Stopping for this sender. Error: {e_loop}"
                    log.error(f"User {user_id}: {error_msg}")
                    errors_this_sender.append(f"Email #{current_email_num}: Sender refused. Stopped.")
                    break # Stop sending from this account
                except Exception as e_loop:
                    error_msg = f"Failed sending email #{current_email_num}. Error: {e_loop}"
                    log.error(f"User {user_id}: [{sender_email}] Error sending email {current_email_num}: {e_loop}")
                    errors_this_sender.append(error_msg)
                    # Optional: Stop if too many errors occur for this sender
                    if len(errors_this_sender) > 5:
                         errors_this_sender.append("Too many consecutive errors, stopping for this sender.")
                         break

            log.info(f"User {user_id}: Finished send loop for {sender_email}. Sent: {sent_count_this_sender}/{count_int}.")

        # --- Connection/Authentication Error Handling (for this sender) ---
        except smtplib.SMTPAuthenticationError:
            error_msg = ("🔑 Authentication failed. Check email/password. "
                       "<i>(Did you use an App Password if needed?)</i>")
            log.error(f"User {user_id}: Authentication failed for {sender_email} on {smtp_server}:{port}.")
            errors_this_sender.append(error_msg)
        except smtplib.SMTPConnectError as e:
            error_msg = f"🔌 Could not connect to <code>{smtp_server}:{port}</code>. Check server/port/firewall. Error: {e}"
            log.error(f"User {user_id}: {error_msg}")
            errors_this_sender.append(error_msg)
        except smtplib.SMTPServerDisconnected:
             error_msg = "🔌 Server disconnected unexpectedly."
             log.error(f"User {user_id}: Server disconnected for {sender_email} at {smtp_server}:{port}.")
             errors_this_sender.append(error_msg)
        except ConnectionRefusedError:
            error_msg = f"🔌 Connection refused by <code>{smtp_server}:{port}</code>."
            log.error(f"User {user_id}: Connection refused for {sender_email} at {smtp_server}:{port}.")
            errors_this_sender.append(error_msg)
        except TimeoutError:
            error_msg = f"⏳ Connection/operation timed out for <code>{smtp_server}:{port}</code>."
            log.error(f"User {user_id}: Timeout for {sender_email} at {smtp_server}:{port}.")
            errors_this_sender.append(error_msg)
        except ssl.SSLError as e:
            error_msg = f"🔒 SSL Error: {e}. (Common if port 465 used without SSL or port 587 without STARTTLS)."
            log.error(f"User {user_id}: SSL Error for {sender_email} at {smtp_server}:{port}. Error: {e}")
            errors_this_sender.append(error_msg)
        except smtplib.SMTPException as e:
             error_msg = f"✉️ SMTP Error: <code>{e}</code>"
             log.error(f"User {user_id}: SMTP Error for {sender_email} at {smtp_server}:{port}. Error: {e}")
             errors_this_sender.append(error_msg)
        except Exception as e:
            error_msg = f"⚙️ Unexpected error: <code>{e}</code>"
            log.exception(f"User {user_id}: An unexpected error occurred for sender {sender_email}: {e}")
            errors_this_sender.append(error_msg)
        finally:
            if server:
                try:
                    server.quit()
                    log.info(f"User {user_id}: SMTP connection closed for {sender_email}.")
                except Exception as e_quit:
                     log.warning(f"User {user_id}: Error during server.quit() for {sender_email}: {e_quit}") # Ignore errors during quit

        # --- Compile results for this sender ---
        sender_time = time.monotonic() - start_time_sender
        result_line = f"{sender_status_prefix} "
        if sent_count_this_sender == count_int:
            result_line += f"✅ Sent all {count_int} emails. ({sender_time:.1f}s)"
        elif sent_count_this_sender > 0:
            result_line += (f"⚠️ Sent {sent_count_this_sender}/{count_int} emails. ({sender_time:.1f}s)\n"
                           f"   Errors:\n   - " + "\n   - ".join(errors_this_sender))
        else: # sent_count_this_sender == 0
            result_line += (f"❌ Failed to send any emails. ({sender_time:.1f}s)\n"
                           f"   Errors:\n   - " + "\n   - ".join(errors_this_sender))
        overall_results_summary.append(result_line)

    # --- Final Summary ---
    total_time = time.monotonic() - start_time_total
    final_message = "🏁 <b>Email Sending Task Complete</b> 🏁\n\n"
    final_message += f"Target: <code>{target_email}</code>\n"
    final_message += f"Requested per Sender: {count_int}\n"
    final_message += f"Total Attempted: {total_attempted}\n"
    final_message += f"<b>Total Successfully Sent: {total_successfully_sent}</b>\n"
    final_message += f"Total Time: {total_time:.2f} seconds\n\n"
    final_message += "<b>--- Sender Results ---</b>\n"
    final_message += "\n\n".join(overall_results_summary) # Use double newline for better separation

    overall_success = total_successfully_sent > 0

    # Log the final outcome
    log.info(f"User {user_id}: Task finished. Overall Success: {overall_success}. Sent: {total_successfully_sent}/{total_attempted}. Time: {total_time:.2f}s")

    return overall_success, final_message


# --- Bot Handlers ---

# /start command
@dp.message_handler(commands=['start'], state='*')
async def cmd_start(message: types.Message, state: FSMContext):
    """Handles the /start command, clears state, and shows the main menu."""
    await state.finish() # Clear any previous state
    user = message.from_user
    log.info(f"User {user.id} ({user.full_name} / @{user.username or 'no_username'}) started the bot.")

    # Use ReplyKeyboardMarkup for persistent buttons
    start_keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False) # False = persistent
    start_keyboard.add(KeyboardButton("📊 Start Report"))
    start_keyboard.add(KeyboardButton("❓ Help"))
    # Consider adding a Cancel button here too if needed
    # start_keyboard.add(KeyboardButton("🚫 Cancel Task"))

    start_msg = f"""⚡️ Welcome {message.from_user.first_name} to 𝕸𝖆𝖎𝖑 𝕱𝖚𝖈𝖐*𝖗 ⚡️
ᴛʜᴇ ᴜʟᴛɪᴍᴀᴛᴇ ꜱᴘᴀᴍ ᴘʟᴀʏɢʀᴏᴜɴᴅ ꜰᴏʀ ꜱᴀᴠᴀɢᴇ ꜱᴇɴᴅᴇʀꜱ.
𝗙𝗢𝗥𝗚𝗘𝗧 𝗥𝗨𝗟𝗘𝗦. 𝗙𝗘𝗔𝗥 𝗡𝗢 𝗙𝗜𝗟𝗧𝗘𝗥𝗦.

━━━━━━━━━━━━━
🔥 𝘽𝙤𝙩 𝘼𝙧𝙨𝙚𝙣𝙖𝙡:
• 𝙎𝙈𝘼𝙎𝙃 𝙄𝙉𝘽𝙊𝙓𝙀𝙎 𝙬𝙞𝙩𝙝 𝙃𝙞𝙜𝙝-𝙑𝙤𝙡𝙪𝙢𝙚 𝘽𝙡𝙖𝙨𝙩𝙨
• Use 𝗠𝗨𝗟𝗧𝗜𝗣𝗟𝗘 sender accounts per run!
• Built-in 𝗗𝗘𝗟𝗔𝗬 system to manage sending rate.
• 𝘽𝙮𝙥𝙖𝙨𝙨 𝘿𝙚𝙩𝙚𝙘𝙩𝙞𝙤𝙣 𝙡𝙞𝙠𝙚 𝙖 𝙂𝙝𝙤𝙨𝙩 (maybe 😉)
• 𝙁𝙖𝙨𝙩. 𝙁𝙚𝙖𝙧𝙡𝙚𝙨𝙨. 𝙁𝙞𝙡𝙩𝙚𝙧-𝙋𝙧𝙤𝙤𝙛 (use responsibly!).

━━━━━━━━━━━━━
⚙️ 𝙃𝙤𝙬 𝙩𝙤 𝙐𝙨𝙚 𝙏𝙝𝙚 𝘽𝙀𝘼𝙎𝙏:
📌 Press '📊 Start Report' to launch your attack.
📌 Tap '❓ Help' to learn all commands.

━━━━━━━━━━━━━
Stay Ruthless. Stay Untouchable.
Bot by @sanam_kinggod (Modified)
"""
    await message.reply(start_msg, reply_markup=start_keyboard)

# /help command (also handles Help button)
@dp.message_handler(Text(equals="❓ Help", ignore_case=True), state='*')
@dp.message_handler(commands=['help'], state='*')
async def cmd_help(message: types.Message, state: FSMContext):
    """Displays help information and cancels any active FSM state."""
    current_state = await state.get_state()
    user_id = message.from_user.id

    # If the user is in the middle of a process, cancel it first
    if current_state is not None:
        log.info(f"User {user_id} used help, cancelling state: {current_state}")
        await state.finish()
        # Send cancellation message separately if triggered by text/button
        if not message.text.startswith('/'):
             await message.reply("ℹ️ Current operation cancelled by requesting help.", reply_markup=ReplyKeyboardRemove())
             # Keep the main keyboard if they pressed the "Help" button
             start_keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
             start_keyboard.add(KeyboardButton("📊 Start Report"))
             start_keyboard.add(KeyboardButton("❓ Help"))
             reply_mk = start_keyboard
        else: # Triggered by /help command
             reply_mk = ReplyKeyboardRemove() # Remove keyboard for command version
    else:
         reply_mk = None # No state, don't change keyboard unless necessary

    help_text = (
        "╭━━━━━━━━━━━━━━━━━━━╮\n"
        "    <b>⚙️ 𝙃𝙀𝙇𝙋 & 𝘾𝙊𝙈𝙈𝘼𝙉𝘿𝙎</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━╯\n\n"

        "<b>📌 USER COMMANDS:</b>\n"
        "📊 <code>/report</code> or <i>'Start Report' Button</i>\n"
        "   ┗ Begins the process to send mass emails.\n"
        f"   Requires <b>Premium Access</b>. Add up to {MAX_SENDER_ACCOUNTS} sender accounts per run.\n"
        f"   Sends up to {MAX_EMAILS_PER_RUN} emails per account with a {INTER_EMAIL_DELAY_SECONDS:.1f}s delay between each.\n\n"

        "❓ <code>/help</code> or <i>'Help' Button</i>\n"
        "   ┗ Shows this message and cancels any current setup process.\n\n"

        "🚫 <code>/cancel</code> or <i>'Cancel Task' Button</i>\n"
        "   ┗ Immediately stops the current email setup process.\n"
    )

    if user_id == OWNER_ID:
        help_text += (
            "\n<b>👑 OWNER COMMANDS:</b>\n"
            "🔑 <code>/addpremium [user_id]</code>\n"
            "   ┗ Grant premium access to a user.\n\n"
            "🔒 <code>/removepremium [user_id]</code>\n"
            "   ┗ Revoke premium access from a user.\n\n"
            "👥 <code>/listpremium</code>\n"
            "   ┗ Show IDs of all premium users.\n"
        )

    help_text += "\n<b>⚠️ Disclaimer:</b> Use this bot responsibly and ethically. Spamming is often illegal and against Terms of Service.\n"
    help_text += "<b>🧠 Bot by:</b> @sanam_kinggod (Modified)"

    await message.reply(help_text, parse_mode="HTML", reply_markup=reply_mk, disable_web_page_preview=True)

# /cancel command (also handles potential Cancel button)
# Let's add a text handler for a potential "Cancel Task" button too
@dp.message_handler(Text(equals="🚫 Cancel Task", ignore_case=True), state='*')
@dp.message_handler(commands=['cancel'], state='*')
async def cmd_cancel(message: types.Message, state: FSMContext):
    """Handles the /cancel command or 'Cancel Task' button, terminating the current FSM state."""
    user_id = message.from_user.id
    current_state = await state.get_state()

    if current_state is None:
        log.info(f"User {user_id} tried to cancel, but no active state.")
        await message.reply(
            "✅ You are not in the middle of any task. Nothing to cancel.",
            reply_markup=ReplyKeyboardRemove() # Clean up if they used the command
        )
         # Re-show main keyboard if they used the button
        if message.text.startswith("🚫"):
            start_keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
            start_keyboard.add(KeyboardButton("📊 Start Report"))
            start_keyboard.add(KeyboardButton("❓ Help"))
            await message.answer("Main Menu:", reply_markup=start_keyboard)
        return

    log.info(f"Cancelling state {current_state} for user {user_id} via cancel command/button.")
    await state.finish()

    # Provide clear feedback and potentially restore the main keyboard
    start_keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    start_keyboard.add(KeyboardButton("📊 Start Report"))
    start_keyboard.add(KeyboardButton("❓ Help"))
    # start_keyboard.add(KeyboardButton("🚫 Cancel Task")) # Can add cancel back if desired

    await message.reply(
        "🚫 <b>Operation Cancelled.</b>\n"
        "Any ongoing setup process has been stopped. You are back at the main menu.",
        reply_markup=start_keyboard,
        parse_mode="HTML"
    )

# --- Owner Commands ---
@dp.message_handler(Command("addpremium"), user_id=OWNER_ID, state="*")
async def cmd_add_premium(message: types.Message):
    """Owner command to grant premium access to a user."""
    args = message.get_args().split()
    if not args or not args[0].isdigit():
        await message.reply(
            "⚠️ <b>Usage:</b> <code>/addpremium &lt;user_id&gt;</code>\n"
            "Example: <code>/addpremium 123456789</code>",
            parse_mode="HTML"
        )
        return

    try:
        user_id_to_add = int(args[0])
    except ValueError:
        await message.reply("⚠️ Invalid User ID. Please provide a numeric Telegram User ID.", parse_mode="HTML")
        return

    if user_id_to_add == OWNER_ID:
        await message.reply("👑 You are the owner, you already have full access.", parse_mode="HTML")
        return
    if user_id_to_add <= 0:
         await message.reply("⚠️ Invalid User ID (must be positive).", parse_mode="HTML")
         return

    if user_id_to_add in premium_users:
        await message.reply(f"ℹ️ User <code>{user_id_to_add}</code> already has premium access.", parse_mode="HTML")
    else:
        premium_users.add(user_id_to_add)
        save_premium_users() # Persist the change
        log.info(f"Owner {message.from_user.id} added premium for {user_id_to_add}")
        await message.reply(
            f"✅ <b>Success!</b>\nUser <code>{user_id_to_add}</code> now has premium access.",
            parse_mode="HTML"
        )
        # Try to notify the user
        try:
            await bot.send_message(
                user_id_to_add,
                "🎉 Congratulations! You have been granted <b>Premium Access</b> to the Mail Sender Bot by the owner!",
                parse_mode="HTML"
            )
            log.info(f"Successfully notified user {user_id_to_add} about premium grant.")
        except BotBlocked:
            log.warning(f"Could not notify user {user_id_to_add} (premium grant): Bot blocked by user.")
            await message.reply(f"(Note: Could not notify user {user_id_to_add} as they may have blocked the bot.)", parse_mode="HTML")
        except Exception as e:
            log.warning(f"Could not notify user {user_id_to_add} about premium grant: {e}")
            await message.reply(f"(Note: Failed to notify user {user_id_to_add} due to an error: {e})", parse_mode="HTML")

@dp.message_handler(Command("removepremium"), user_id=OWNER_ID, state="*")
async def cmd_remove_premium(message: types.Message):
    """Owner command to revoke premium access."""
    args = message.get_args().split()
    if not args or not args[0].isdigit():
        await message.reply(
            "⚠️ <b>Usage:</b> <code>/removepremium &lt;user_id&gt;</code>\n"
            "Example: <code>/removepremium 987654321</code>",
            parse_mode="HTML"
        )
        return

    try:
        user_id_to_remove = int(args[0])
    except ValueError:
        await message.reply("⚠️ Invalid User ID. Please provide a numeric Telegram User ID.", parse_mode="HTML")
        return

    if user_id_to_remove == OWNER_ID:
        await message.reply("⛔️ Cannot remove the owner's implicit access.", parse_mode="HTML")
        return
    if user_id_to_remove <= 0:
         await message.reply("⚠️ Invalid User ID (must be positive).", parse_mode="HTML")
         return

    if user_id_to_remove in premium_users:
        premium_users.discard(user_id_to_remove) # Use discard to avoid error if not present
        save_premium_users() # Persist the change
        log.info(f"Owner {message.from_user.id} removed premium for {user_id_to_remove}")
        await message.reply(
            f"❌ <b>Premium access revoked</b> for user <code>{user_id_to_remove}</code>.",
            parse_mode="HTML"
        )
        # Try to notify the user
        try:
            await bot.send_message(
                user_id_to_remove,
                "ℹ️ Your <b>Premium Access</b> to the Mail Sender Bot has been revoked by the owner.",
                parse_mode="HTML"
            )
            log.info(f"Successfully notified user {user_id_to_remove} about premium removal.")
        except BotBlocked:
             log.warning(f"Could not notify user {user_id_to_remove} (premium removal): Bot blocked by user.")
             await message.reply(f"(Note: Could not notify user {user_id_to_remove} as they may have blocked the bot.)", parse_mode="HTML")
        except Exception as e:
            log.warning(f"Could not notify user {user_id_to_remove} about premium removal: {e}")
            await message.reply(f"(Note: Failed to notify user {user_id_to_remove} due to an error: {e})", parse_mode="HTML")
    else:
        await message.reply(f"⚠️ User <code>{user_id_to_remove}</code> does not currently have premium access.", parse_mode="HTML")

@dp.message_handler(Command("listpremium"), user_id=OWNER_ID, state="*")
async def cmd_list_premium(message: types.Message):
    """Owner command to list all premium users."""
    if not premium_users:
        await message.reply(
            "📭 Currently, no users have explicit premium access (besides you, the owner).",
            parse_mode="HTML"
        )
        return

    user_list = "\n".join([f"• <code>{uid}</code>" for uid in sorted(list(premium_users))])
    count = len(premium_users)
    await message.reply(
        f"👥 <b>Premium Users ({count}):</b>\n{user_list}",
        parse_mode="HTML",
        disable_web_page_preview=True # Avoid potential previews if IDs look like links
    )

# --- Report Command and FSM Handlers ---

# Step 0: Initiate Report (/report command or "📊 Start Report" button)
@dp.message_handler(Text(equals="📊 Start Report", ignore_case=True), state=None)
@dp.message_handler(commands=['report'], state=None)
async def cmd_report_start(message: types.Message, state: FSMContext):
    """Starts the email report configuration process."""
    user = message.from_user
    if not is_allowed_user(user):
        log.warning(f"Unauthorized /report attempt by {user.id} ({user.full_name} / @{user.username or 'no_username'})")
        await message.reply("🚫 Access Denied: This feature requires <b>Premium Access</b>. Please contact the owner if you believe this is an error.",
                            reply_markup=ReplyKeyboardRemove()) # Remove keyboard for non-premium
        return

    log.info(f"User {user.id} starting /report process.")
    # Initialize the list for sender accounts in the state
    await state.update_data(sender_accounts=[])
    # Start by asking for the first email
    await ReportForm.waiting_for_email.set()
    await message.reply("🚀 Okay, let's configure the mass email report.\n\n"
                        "<b>Step 1a: Sender Account 1</b>\n"
                        "📧 Enter the <b>first</b> sender email address (e.g., <code>you@gmail.com</code>):\n\n"
                        "<i>(Type /cancel anytime to stop this process)</i>",
                        reply_markup=ReplyKeyboardRemove()) # Remove main keyboard during FSM

# Step 1a: Get Sender Email
@dp.message_handler(state=ReportForm.waiting_for_email, content_types=types.ContentType.TEXT)
async def process_email(message: types.Message, state: FSMContext):
    """Receives and validates the sender's email address."""
    email_text = message.text.strip()
    # Simple validation (can be improved with regex if needed)
    if '@' not in email_text or '.' not in email_text.split('@')[-1] or ' ' in email_text or len(email_text) < 6:
         await message.reply("⚠️ Invalid format. Please enter a valid email address (e.g., <code>name@domain.com</code>).")
         return # Remain in the same state

    # Temporarily store the current email being added
    await state.update_data(current_email=email_text)
    await ReportForm.waiting_for_password.set() # Move to password state
    await message.reply(f"📧 Email: <code>{email_text}</code>\n\n"
                        "<b>Step 1b:</b> 🔑 Enter the <b>password</b> or <b>App Password</b> for this email account.\n\n"
                        "<b>⚠️ SECURITY: This message containing your password will be deleted automatically.</b>")
    # Don't delete the user's email input, only the password input later

# Step 1b: Get Sender Password
@dp.message_handler(state=ReportForm.waiting_for_password, content_types=types.ContentType.TEXT)
async def process_password(message: types.Message, state: FSMContext):
    """Receives the password, stores the email/password pair, and asks about adding more accounts."""
    password_text = message.text # Don't strip passwords, they might have spaces

    if not password_text: # Basic check if password is empty
        # Delete the user's empty message
        await delete_message_safely(message)
        await message.reply("❌ Password cannot be empty. Please try entering the password again.")
        return # Remain in the same state

    # Get the email stored temporarily and the list of accounts
    user_data = await state.get_data()
    current_email = user_data.get('current_email')
    sender_accounts = user_data.get('sender_accounts', [])

    if not current_email:
        log.error(f"User {message.from_user.id}: State error - current_email missing when processing password.")
        await state.finish()
        await message.reply("❌ An internal error occurred (missing email). Please start over with /report.", reply_markup=ReplyKeyboardRemove())
        await delete_message_safely(message)
        return

    # Add the new account details to the list
    sender_accounts.append({'email': current_email, 'password': password_text})
    await state.update_data(sender_accounts=sender_accounts, current_email=None) # Clear temporary email

    log.info(f"User {message.from_user.id}: Added sender account #{len(sender_accounts)}. Email: {current_email}")

    # Delete the password message **after** processing it
    await delete_message_safely(message)

    # Check if max accounts reached
    if len(sender_accounts) >= MAX_SENDER_ACCOUNTS:
        log.info(f"User {message.from_user.id}: Reached max sender accounts ({MAX_SENDER_ACCOUNTS}). Moving to SMTP setup.")
        await ReportForm.waiting_for_smtp_server.set()
        await message.answer(f"✅ Sender account #{len(sender_accounts)} added.\n"
                            f"You have reached the maximum of {MAX_SENDER_ACCOUNTS} sender accounts for this run.\n\n"
                            "<b>Step 2: SMTP Server</b>\n"
                            "🖥️ Enter the SMTP server address (e.g., <code>smtp.gmail.com</code>, <code>smtp.office365.com</code>):")
    else:
        # Ask if they want to add more
        await ReportForm.ask_more_accounts.set()
        keyboard = InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            InlineKeyboardButton("➕ Add Another Account", callback_data="add_more_account"),
            InlineKeyboardButton("✅ Done Adding Accounts", callback_data="done_adding_accounts")
        )
        await message.answer(f"✅ Sender account #{len(sender_accounts)} (<code>{current_email}</code>) added successfully.\n\n"
                            f"Do you want to add another sender account? (Max: {MAX_SENDER_ACCOUNTS})",
                            reply_markup=keyboard)

# Step 1c: Ask More Accounts (Callback Query Handler)
@dp.callback_query_handler(state=ReportForm.ask_more_accounts)
async def process_ask_more_accounts(callback_query: types.CallbackQuery, state: FSMContext):
    """Handles the response to whether the user wants to add more sender accounts."""
    await callback_query.answer() # Acknowledge the button press

    if callback_query.data == "add_more_account":
        # Go back to asking for email
        await ReportForm.waiting_for_email.set()
        await callback_query.message.edit_text(
            f"Okay, let's add the next sender account.\n\n"
            f"<b>Step 1a: Sender Account #{len((await state.get_data()).get('sender_accounts', [])) + 1}</b>\n"
            f"📧 Enter the next sender email address:")
    elif callback_query.data == "done_adding_accounts":
        # Proceed to SMTP server details
        await ReportForm.waiting_for_smtp_server.set()
        user_data = await state.get_data()
        sender_count = len(user_data.get('sender_accounts', []))
        await callback_query.message.edit_text(
            f"👍 Great! You've added {sender_count} sender account(s).\n\n"
            "<b>Step 2: SMTP Server</b>\n"
            "🖥️ Enter the SMTP server address (e.g., <code>smtp.gmail.com</code>, <code>smtp.office365.com</code>):")
    else:
        # Should not happen with the defined buttons
        log.warning(f"User {callback_query.from_user.id}: Received unexpected callback data '{callback_query.data}' in state ask_more_accounts.")
        await callback_query.message.edit_text("🤔 Unexpected response. Please choose one of the buttons.")


# Step 2: Get SMTP Server
@dp.message_handler(state=ReportForm.waiting_for_smtp_server, content_types=types.ContentType.TEXT)
async def process_smtp_server(message: types.Message, state: FSMContext):
    """Receives and validates the SMTP server address."""
    smtp_server_text = message.text.strip().lower() # Store lowercase for consistency
    # Basic validation
    if not smtp_server_text or ' ' in smtp_server_text or '.' not in smtp_server_text or len(smtp_server_text) < 4:
        await message.reply("⚠️ Please enter a valid SMTP server address (e.g., <code>smtp.example.com</code>).")
        return
    await state.update_data(smtp_server=smtp_server_text)
    await ReportForm.waiting_for_smtp_port.set() # Use next() equivalent
    await message.reply(f"🖥️ SMTP Server: <code>{smtp_server_text}</code>\n\n"
                        "<b>Step 3: SMTP Port</b>\n"
                        "🔌 Enter the SMTP port number (e.g., <code>587</code> for TLS, <code>465</code> for SSL):")

# Step 3: Get SMTP Port
@dp.message_handler(state=ReportForm.waiting_for_smtp_port, content_types=types.ContentType.TEXT)
async def process_smtp_port(message: types.Message, state: FSMContext):
    """Receives and validates the SMTP port."""
    port_text = message.text.strip()
    if not port_text.isdigit():
        await message.reply("❌ Port must be a number (e.g., <code>587</code> or <code>465</code>).")
        return
    try:
        port_int = int(port_text)
        if not 1 <= port_int <= 65535:
            await message.reply("❌ Port number must be between 1 and 65535.")
            return
    except ValueError:
        await message.reply("❌ Invalid number format for port.")
        return

    await state.update_data(smtp_port=port_int)
    await ReportForm.waiting_for_target_email.set()
    await message.reply(f"🔌 SMTP Port: <code>{port_int}</code>\n\n"
                        "<b>Step 4: Target Recipient</b>\n"
                        "🎯 Enter the <b>single</b> target email address where all emails will be sent:")

# Step 4: Get Target Email
@dp.message_handler(state=ReportForm.waiting_for_target_email, content_types=types.ContentType.TEXT)
async def process_target_email(message: types.Message, state: FSMContext):
    """Receives and validates the target email address."""
    target_email_text = message.text.strip()
    # Simple validation
    if '@' not in target_email_text or '.' not in target_email_text.split('@')[-1] or ' ' in target_email_text or len(target_email_text) < 6:
        await message.reply("⚠️ Please enter a valid single target email address.")
        return
    await state.update_data(target_email=target_email_text)
    await ReportForm.waiting_for_subject.set()
    await message.reply(f"🎯 Target Email: <code>{target_email_text}</code>\n\n"
                        "<b>Step 5: Email Subject</b>\n"
                        "📝 Enter the subject line for the emails:")

# Step 5: Get Subject
@dp.message_handler(state=ReportForm.waiting_for_subject, content_types=types.ContentType.TEXT)
async def process_subject(message: types.Message, state: FSMContext):
    """Receives the email subject line."""
    subject_text = message.text.strip()
    if not subject_text:
        await message.reply("❌ Subject cannot be empty. Please enter a subject line.")
        return
    await state.update_data(subject=subject_text)
    await ReportForm.waiting_for_body.set()
    await message.reply(f"📝 Subject: <code>{subject_text}</code>\n\n"
                        "<b>Step 6: Email Body</b>\n"
                        "📋 Enter the main content (body) of the email:")

# Step 6: Get Body
@dp.message_handler(state=ReportForm.waiting_for_body, content_types=types.ContentType.TEXT)
async def process_body(message: types.Message, state: FSMContext):
    """Receives the email body content."""
    body_text = message.text # Allow multiline, don't strip leading/trailing whitespace aggressively unless intended
    if not body_text.strip(): # Check if it's effectively empty
        await message.reply("❌ Body cannot be empty. Please enter the email content.")
        return
    await state.update_data(body=body_text)
    await ReportForm.waiting_for_count.set()
    await message.reply("📋 Email body captured.\n\n"
                        f"<b>Step 7: Email Count</b>\n"
                        f"🔢 Enter how many emails to send <b>from each</b> sender account (1-{MAX_EMAILS_PER_RUN}):")

# Step 7: Get Count
@dp.message_handler(state=ReportForm.waiting_for_count, content_types=types.ContentType.TEXT)
async def process_count(message: types.Message, state: FSMContext):
    """Receives and validates the number of emails to send per sender."""
    count_text = message.text.strip()
    if not count_text.isdigit():
        await message.reply(f"❌ Please enter a valid number between 1 and {MAX_EMAILS_PER_RUN}.")
        return
    try:
        count_int = int(count_text)
        if not 1 <= count_int <= MAX_EMAILS_PER_RUN:
            await message.reply(f"❌ Count must be between 1 and {MAX_EMAILS_PER_RUN}.")
            return
    except ValueError:
        await message.reply(f"❌ Invalid number format. Enter a number between 1 and {MAX_EMAILS_PER_RUN}.")
        return

    await state.update_data(count=count_int)
    user_data = await state.get_data()

    # --- Display Confirmation ---
    sender_emails = [acc['email'] for acc in user_data.get('sender_accounts', [])]
    sender_list_str = "\n".join([f"  • <code>{email}</code>" for email in sender_emails])
    if not sender_list_str: sender_list_str = "<i>None configured!</i>"

    confirmation_text = (
        f"<b>✨ Final Confirmation ✨</b>\n\n"
        f"Please review the details before sending:\n\n"
        f"<b>Sender Accounts ({len(sender_emails)}):</b>\n{sender_list_str}\n\n"
        f"<b>SMTP Server:</b> <code>{user_data.get('smtp_server', 'N/A')}</code>\n"
        f"<b>SMTP Port:</b> <code>{user_data.get('smtp_port', 'N/A')}</code>\n\n"
        f"<b>Target Recipient:</b> <code>{user_data.get('target_email', 'N/A')}</code>\n"
        f"<b>Subject:</b> <code>{user_data.get('subject', 'N/A')}</code>\n"
        f"<b>Emails per Sender:</b> <code>{count_int}</code>\n"
        f"<b>Total Emails to Send:</b> <code>{count_int * len(sender_emails)}</code>\n"
        f"<b>Delay Between Sends:</b> {INTER_EMAIL_DELAY_SECONDS:.1f} seconds\n\n"
        f"⚠️ <b>Warning:</b> Sending large volumes of email may violate terms of service or laws. Proceed responsibly.\n\n"
        f"Ready to launch the email barrage?"
    )

    confirm_keyboard = InlineKeyboardMarkup(row_width=2)
    confirm_keyboard.add(
        InlineKeyboardButton("✅ Yes, Send Now!", callback_data="confirm_send"),
        InlineKeyboardButton("❌ No, Cancel", callback_data="cancel_send")
    )

    await ReportForm.waiting_for_confirmation.set()
    await message.reply(confirmation_text, reply_markup=confirm_keyboard)

# Step 8: Handle Confirmation Buttons (Callback Query)
@dp.callback_query_handler(state=ReportForm.waiting_for_confirmation)
async def process_confirmation(callback_query: types.CallbackQuery, state: FSMContext):
    """Handles the final confirmation buttons (Send or Cancel)."""
    user_id = callback_query.from_user.id
    await callback_query.answer() # Acknowledge button press

    if callback_query.data == "cancel_send":
        log.info(f"User {user_id} cancelled the email task at confirmation.")
        await state.finish()
        try:
            # Restore main keyboard after cancellation
            start_keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
            start_keyboard.add(KeyboardButton("📊 Start Report"))
            start_keyboard.add(KeyboardButton("❓ Help"))
            await callback_query.message.edit_text("❌ Operation cancelled by user.", reply_markup=None) # Remove inline buttons
            await callback_query.message.answer("Main menu:", reply_markup=start_keyboard) # Show main keyboard
        except MessageNotModified:
            pass # Ignore if message is already edited
        except Exception as e:
            log.error(f"Error editing message after cancel confirmation: {e}")
        return

    if callback_query.data == "confirm_send":
        log.info(f"User {user_id} confirmed the email task. Initiating sending process.")
        # Edit the message to show "Sending..." immediately
        try:
            status_message = await callback_query.message.edit_text(
                "🚀 <b>Initiating Email Sending Process...</b>\n\n"
                "Please wait. This may take some time depending on the number of emails and delays.\n"
                "You will receive a final report here.",
                reply_markup=None # Remove inline buttons
            )
        except MessageNotModified:
             # If the message is already "Initiating...", get the message object anyway
             status_message = callback_query.message
        except Exception as e:
             log.error(f"Error editing message to 'Initiating...': {e}")
             # Try sending a new message if editing fails
             await callback_query.message.answer("🚀 Initiating Email Sending Process...")
             # In this case, we can't easily update progress on the original message.
             # We'll send the final result as a new message later.
             status_message = None # Indicate we can't update the original message

        user_data = await state.get_data()
        await state.finish() # Finish FSM state *before* starting the long task

        # Start the potentially long email sending task
        success, final_message = await send_emails_async(user_data, user_id, status_message)

        # Send the final result
        try:
            if status_message: # If we could edit the original message
                 await status_message.edit_text(final_message, disable_web_page_preview=True)
            else: # If editing failed earlier, send the result as a new message
                 await bot.send_message(callback_query.message.chat.id, final_message, disable_web_page_preview=True)

            # Restore main keyboard after completion
            start_keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
            start_keyboard.add(KeyboardButton("📊 Start Report"))
            start_keyboard.add(KeyboardButton("❓ Help"))
            await bot.send_message(callback_query.message.chat.id, "Return to main menu:", reply_markup=start_keyboard)

        except MessageNotModified:
            log.debug("Final result message was the same as the previous status.")
        except Exception as e:
            log.error(f"Error sending/editing final result message for user {user_id}: {e}")
            # Try sending as a fallback if edit failed
            try:
                 await bot.send_message(callback_query.message.chat.id, final_message, disable_web_page_preview=True)
            except Exception as e2:
                 log.error(f"Fallback sending of final result also failed for user {user_id}: {e2}")


# --- Catch-all for unexpected text messages during FSM ---
@dp.message_handler(state='*', content_types=types.ContentType.ANY)
async def handle_unexpected_input(message: types.Message, state: FSMContext):
    """Handles unexpected input types or text when the bot expects specific FSM input."""
    current_state = await state.get_state()
    log.warning(f"User {message.from_user.id} sent unexpected input '{message.text or message.content_type}' in state {current_state}")

    state_map = {
        ReportForm.waiting_for_email.state: "an email address",
        ReportForm.waiting_for_password.state: "a password",
        ReportForm.waiting_for_smtp_server.state: "an SMTP server address",
        ReportForm.waiting_for_smtp_port.state: "an SMTP port number",
        ReportForm.waiting_for_target_email.state: "a target email address",
        ReportForm.waiting_for_subject.state: "an email subject",
        ReportForm.waiting_for_body.state: "the email body text",
        ReportForm.waiting_for_count.state: "a number (how many emails)",
        ReportForm.ask_more_accounts.state: "a button click ('Add Another' or 'Done')",
        ReportForm.waiting_for_confirmation.state: "a button click ('Send' or 'Cancel')",
    }
    expected_input = state_map.get(current_state, "specific input for the current step")

    await message.reply(f"⚠️ Unexpected input. I was expecting {expected_input}.\n"
                        f"Please provide the requested information, or type /cancel to stop.")
    # Optionally delete the user's unexpected message
    # await delete_message_safely(message)


# --- Main Execution ---
if __name__ == '__main__':
    log.info("Bot starting...")
    # Load premium users from file on startup
    load_premium_users()
    log.info(f"Owner ID set to: {OWNER_ID}")
    log.info(f"Premium users loaded: {premium_users if premium_users else 'None'}")
    log.info(f"Max emails per run: {MAX_EMAILS_PER_RUN}, Max sender accounts: {MAX_SENDER_ACCOUNTS}, Delay: {INTER_EMAIL_DELAY_SECONDS}s")

    # Start polling
    log.info("Starting polling...")
    async def on_startup(dispatcher):
        log.info("Bot polling started successfully!")

    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
    # on_shutdown can be added too if cleanup is needed