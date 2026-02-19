import os
import logging
import re
import asyncio
from decimal import Decimal, ROUND_DOWN
from telegram.ext import Application, MessageHandler, filters, CommandHandler
import metaapi_cloud_sdk as metaapi

# ========== CONFIGURATION ==========
METAAPI_TOKEN = os.environ.get('METAAPI_TOKEN')
ACCOUNT_ID = os.environ.get('ACCOUNT_ID')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
RISK_PERCENT = float(os.environ.get('RISK_PERCENT', '1.0'))
ALLOWED_USERS = os.environ.get('ALLOWED_USERS', '').split(',')
PORT = int(os.environ.get('PORT', 10000))
RENDER_EXTERNAL_URL = os.environ.get('RENDER_EXTERNAL_URL', 'https://t4trade-telegram-bot.onrender.com')
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== METAAPI SETUP (async) ==========
async def init_metaapi(application):
    """Initialize MetaAPI client and account, store in bot_data."""
    api_client = metaapi.MetaApi(METAAPI_TOKEN)
    account = await api_client.metatrader_account_api.get_account(ACCOUNT_ID)
    application.bot_data['api_client'] = api_client
    application.bot_data['account'] = account
    logger.info("MetaAPI initialized")

# ========== TELEGRAM COMMANDS ==========
async def start(update, context):
    user = update.effective_user.username
    if user not in ALLOWED_USERS and ALLOWED_USERS != ['']:
        await update.message.reply_text("You are not authorized to use this bot.")
        return
    await update.message.reply_text(
        f"✅ Bot connected to T4Trade\n"
        f"Account: {ACCOUNT_ID}\n"
        f"Risk: {RISK_PERCENT}%\n\n"
        f"Send a signal like:\n"
        f"BUY EURUSD 1.12345 SL 1.12000 TP 1.13000"
    )

async def balance(update, context):
    try:
        account = context.bot_data['account']
        await account.wait_connected()
        info = await account.get_account_information()
        await update.message.reply_text(
            f"💰 Balance: ${info.balance:.2f}\n"
            f"📊 Equity: ${info.equity:.2f}\n"
            f"📉 Free Margin: ${info.margin_free:.2f}"
        )
    except Exception as e:
        await update.message.reply_text(f"Error fetching balance: {str(e)}")

def parse_signal(text):
    text = text.upper().strip()
    action_match = re.search(r'\b(BUY|SELL)\b', text)
    if not action_match:
        return None
    action = action_match.group(1)
    
    symbol_match = re.search(r'\b([A-Z]{6})\b', text)
    if not symbol_match:
        symbol_match = re.search(r'\b(XAU|XAG|BTC|ETH)\b', text)
    if not symbol_match:
        return None
    symbol = symbol_match.group(1)
    
    entry_match = re.search(r'\b(\d+\.\d+)\b', text)
    entry = float(entry_match.group(1)) if entry_match else None
    
    sl_match = re.search(r'SL\s*(\d+\.\d+)', text)
    sl = float(sl_match.group(1)) if sl_match else None
    
    tp_match = re.search(r'TP\s*(\d+\.\d+)', text)
    tp = float(tp_match.group(1)) if tp_match else None
    
    return {
        'action': action,
        'symbol': symbol,
        'entry': entry,
        'sl': sl,
        'tp': tp
    }

def calculate_lot_size(balance, risk_percent, entry, sl, symbol_info):
    if not sl or not entry:
        return symbol_info['volume_min']
    risk_amount = balance * (risk_percent / 100)
    point_value = symbol_info['point_value']
    points_at_risk = abs(entry - sl) / symbol_info['point_size']
    raw_lot = risk_amount / (points_at_risk * point_value)
    lot_step = symbol_info['volume_step']
    lot = Decimal(str(raw_lot)).quantize(Decimal(str(lot_step)), rounding=ROUND_DOWN)
    lot = max(float(lot), symbol_info['volume_min'])
    lot = min(lot, symbol_info['volume_max'])
    return lot

async def handle_signal(update, context):
    user = update.effective_user.username
    if user not in ALLOWED_USERS and ALLOWED_USERS != ['']:
        await update.message.reply_text("⛔ Unauthorized")
        return
    
    text = update.message.text
    logger.info(f"Received: {text}")
    
    signal = parse_signal(text)
    if not signal:
        await update.message.reply_text("❌ Could not parse signal. Use format:\nBUY EURUSD 1.12345 SL 1.12000 TP 1.13000")
        return
    
    try:
        account = context.bot_data['account']
        await account.wait_connected()
        account_info = await account.get_account_information()
        symbol_spec = await account.get_symbol_specification(signal['symbol'])
        
        if not signal['entry']:
            price = await account.get_current_price(signal['symbol'])
            signal['entry'] = price['ask'] if signal['action'] == 'BUY' else price['bid']
        
        if signal['sl']:
            symbol_info = {
                'point_size': symbol_spec['pointSize'],
                'point_value': await account.get_point_value(signal['symbol'], account_info.balance_currency),
                'volume_min': symbol_spec['volumeMin'],
                'volume_max': symbol_spec['volumeMax'],
                'volume_step': symbol_spec['volumeStep']
            }
            lot = calculate_lot_size(account_info.balance, RISK_PERCENT,
                                    signal['entry'], signal['sl'], symbol_info)
        else:
            lot = symbol_spec['volumeMin']
        
        order_type = 'ORDER_TYPE_BUY' if signal['action'] == 'BUY' else 'ORDER_TYPE_SELL'
        current_price = await account.get_current_price(signal['symbol'])
        price = current_price['ask'] if signal['action'] == 'BUY' else current_price['bid']
        
        order = {
            'symbol': signal['symbol'],
            'orderType': order_type,
            'volume': lot,
            'price': price,
            'stopLoss': signal['sl'],
            'takeProfit': signal['tp'],
            'comment': 'Telegram Signal'
        }
        
        result = await account.create_market_order(order)
        
        await update.message.reply_text(
            f"✅ Trade placed!\n"
            f"{signal['action']} {lot} {signal['symbol']} @ {price:.5f}\n"
            f"SL: {signal['sl']} | TP: {signal['tp']}\n"
            f"Risk: ${account_info.balance * RISK_PERCENT/100:.2f} ({RISK_PERCENT}%)"
        )
        
    except Exception as e:
        logger.error(f"Trade error: {str(e)}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

def main():
    # Create application
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(init_metaapi).build()
    
    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_signal))
    
    # Set up webhook
    webhook_url = f"{RENDER_EXTERNAL_URL}/webhook"
    logger.info(f"Starting webhook on {webhook_url}")
    
    # Start webhook
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )
if __name__ == '__main__':
    main()
