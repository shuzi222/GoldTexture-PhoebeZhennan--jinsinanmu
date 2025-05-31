import okx.Trade as Trade
import okx.MarketData as MarketData
import okx.Account as Account
import okx.PublicData as PublicData
from okx.websocket.WsPrivateAsync import WsPrivateAsync
import pandas as pd
import talib
import dearpygui.dearpygui as dpg
import json
import os
import time
import threading
import gc
import numpy as np
from datetime import datetime, timedelta
from queue import Queue
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import traceback
import sys

# 强制设置 UTF-8 编码
os.environ['PYTHONIOENCODING'] = 'utf-8'
if sys.platform == 'win32':
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding='utf-8')
    else:
        logging.warning("sys.stdout is None, skipping stdout reconfiguration")
    if sys.stderr is not None:
        sys.stderr.reconfigure(encoding='utf-8')
    else:
        logging.warning("sys.stderr is None, skipping stderr reconfiguration")

# 设置日志
log_handler = RotatingFileHandler('websocket.log', maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logging.basicConfig(handlers=[log_handler], level=logging.INFO)

# API 密钥存储文件
CONFIG_FILE = 'okx_config.json'

# 全局变量
trade_client = None
market_client = None
account_client = None
public_client = None
ws = None
rsi_enabled = False
macd_enabled = False
rsi_buy_ratio = 0.0
macd_buy_ratio = 0.0
rsi_timeframe = '1h'
macd_timeframe = '1h'
rsi_buy_value = 30.0
rsi_sell_value = 70.0
last_rsi_buy_time = None
last_rsi_sell_time = None
last_macd_buy_time = None
last_macd_sell_time = None
current_price = 0.0
latest_rsi = None
latest_macd = None
latest_signal = None
latest_histogram = None
running = False
lock = threading.Lock()
animation_frame = 0
price_update_time = None
update_queue = Queue()
ws_connected = False
order_updates = Queue()
loop = None
last_message_time = None
last_ui_log_time = 0
last_ws_log_time = 0
request_timestamps = []
REQUEST_LIMIT = 3
REQUEST_WINDOW = 2.0

# 时间框架映射
timeframe_display_map = {
    '1分钟': '1m', '5分钟': '5m', '15分钟': '15m', '30分钟': '30m',
    '1小时': '1h', '4小时': '4h', '1日': '1d'
}
timeframe_reverse_map = {v: k for k, v in timeframe_display_map.items()}

def rate_limit():
    with lock:
        current_time = time.time()
        request_timestamps[:] = [t for t in request_timestamps if current_time - t < REQUEST_WINDOW]
        if len(request_timestamps) >= REQUEST_LIMIT:
            sleep_time = REQUEST_WINDOW - (current_time - request_timestamps[0])
            if sleep_time > 0:
                time.sleep(sleep_time)
            request_timestamps[:] = [t for t in request_timestamps if current_time - t < REQUEST_WINDOW]
        request_timestamps.append(current_time)

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if not content:
                    return {}
                return json.loads(content)
        except Exception as e:
            logging.error(f"加载配置文件失败: {str(e)}")
            return {}
    return {}

def save_config(api_key, api_secret, passphrase):
    config = {'api_key': api_key, 'api_secret': api_secret, 'passphrase': passphrase}
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
        logging.info("配置文件已保存")
    except Exception as e:
        logging.error(f"保存配置文件失败: {str(e)}")

def init_okx(api_key, api_secret, passphrase, flag='0'):
    global trade_client, market_client, account_client, public_client
    try:
        trade_client = Trade.TradeAPI(api_key, api_secret, passphrase, use_server_time=False, flag=flag)
        market_client = MarketData.MarketAPI(flag=flag)
        account_client = Account.AccountAPI(api_key, api_secret, passphrase, use_server_time=False, flag=flag)
        public_client = PublicData.PublicAPI(flag=flag)
        response = account_client.get_account_balance()
        if response.get('code') != '0':
            raise Exception(f"账户余额检查失败: {response.get('msg', '未知错误')}")
        logging.info(f"OKX API 初始化成功，flag={flag}")
        return True
    except Exception as e:
        update_queue.put(("status_label", f"OKX API 初始化失败: {str(e)}"))
        logging.error(f"OKX API 初始化失败: {str(e)}\n{traceback.format_exc()}")
        return False

def ws_callback(message):
    global current_price, price_update_time, ws_connected, last_message_time, last_ws_log_time
    with lock:
        last_message_time = datetime.now()
        try:
            data = json.loads(message)
            current_time = time.time()
            if current_time - last_ws_log_time >= 10:
                logging.debug(f"WebSocket 消息: {data}")
                last_ws_log_time = current_time
            event = data.get('event')
            if event == 'login':
                if data.get('code') == '0':
                    ws_connected = True
                    update_queue.put(("status_label", "WebSocket 已连接"))
                    logging.info(f"WebSocket 登录成功: {data}")
                else:
                    ws_connected = False
                    update_queue.put(("status_label", f"WebSocket 登录失败: {data.get('msg', '')}"))
                    logging.error(f"WebSocket 登录失败: {data}")
            elif event == 'channel-connection-count':
                logging.info(f"频道连接数: {data}")
            elif event == 'channel-connection-error':
                ws_connected = False
                update_queue.put(("status_label", f"连接限制: {data.get('channel')}"))
                logging.error(f"超过连接限制: {data}")
            elif event == 'notice':
                update_queue.put(("status_label", f"WebSocket 通知: {data.get('msg', '')}"))
                logging.warning(f"WebSocket 通知: {data}")
                if data.get('code') == '64008':
                    ws_connected = False
            elif event == 'pong':
                if current_time - last_ws_log_time >= 10:
                    logging.debug("收到 pong")
                    last_ws_log_time = current_time
            elif data.get('arg', {}).get('channel') == 'bbo-tbt':
                try:
                    price = float(data['data'][0]['bids'][0][0])
                    current_price = price
                    price_update_time = datetime.now()
                    update_queue.put(("price_label", f"当前 BTC 价格: ${price:.2f}", (255, 255, 255)))
                    logging.info(f"WebSocket 价格更新: ${price:.2f}")
                except Exception as e:
                    logging.error(f"WebSocket 价格更新错误: {str(e)}")
            elif data.get('arg', {}).get('channel') == 'orders':
                order_updates.put(data)
        except Exception as e:
            logging.error(f"WebSocket 回调错误: {str(e)}\n{traceback.format_exc()}")

async def init_websocket(api_key, api_secret, passphrase):
    global ws, ws_connected, last_message_time
    last_message_time = datetime.now()
    try:
        ws = WsPrivateAsync(
            apiKey=api_key,
            secretKey=api_secret,
            passphrase=passphrase,
            url="wss://ws.okx.com:8443/ws/v5/private",
            useServerTime=False
        )
        await ws.start()
        logging.info("WebSocket 已启动")
        args = [
            {"channel": "bbo-tbt", "instId": "BTC-USDT"},
            {"channel": "orders", "instType": "SPOT", "instId": "BTC-USDT"}
        ]
        await ws.subscribe(args, callback=ws_callback)
        logging.info("已订阅 bbo-tbt 和 orders 频道")
        while True:
            if not ws_connected:
                update_queue.put(("status_label", "WebSocket 断开，尝试重连"))
                logging.warning("WebSocket 断开，尝试重连")
                await ws.start()
                await ws.subscribe(args, callback=ws_callback)
                last_message_time = datetime.now()
            if last_message_time and (datetime.now() - last_message_time).total_seconds() > 60:
                ws_connected = False
                update_queue.put(("status_label", "WebSocket 无响应，尝试重连"))
                logging.warning("WebSocket 60秒无响应")
            await asyncio.sleep(5)
    except Exception as e:
        ws_connected = False
        update_queue.put(("status_label", f"WebSocket 连接失败: {str(e)}"))
        logging.error(f"WebSocket 连接失败: {str(e)}\n{traceback.format_exc()}")
        await asyncio.sleep(5)
        await init_websocket(api_key, api_secret, passphrase)

def start_websocket(api_key, api_secret, passphrase):
    global loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(init_websocket(api_key, api_secret, passphrase))
    loop.run_forever()

def get_server_time():
    try:
        response = public_client.get_system_time()
        if response.get('code') == '0':
            server_time = int(response['data'][0]['ts'])
            return datetime.fromtimestamp(server_time / 1000.0)
        else:
            logging.error(f"获取服务器时间失败: {response.get('msg', '未知错误')}")
            return datetime.now()
    except Exception as e:
        logging.error(f"获取服务器时间失败: {str(e)}")
        return datetime.now()

def get_btc_price():
    global current_price, price_update_time
    rate_limit()
    with lock:
        if ws_connected and current_price > 0:
            return current_price
    try:
        ticker = market_client.get_ticker(instId='BTC-USDT')
        if ticker.get('code') != '0':
            raise Exception(f"获取行情失败: {ticker.get('msg', '未知错误')}")
        with lock:
            current_price = float(ticker['data'][0]['last'])
            price_update_time = datetime.now()
        return current_price
    except Exception as e:
        update_queue.put(("status_label", f"获取价格失败: {str(e)}"))
        logging.error(f"获取价格失败: {str(e)}")
        return None

def get_klines(symbol, interval, limit=100):
    rate_limit()
    try:
        timeframe_map = {'1m': '1m', '5m': '5m', '15m': '15m', '30m': '30m', '1h': '1H', '4h': '4H', '1d': '1D'}
        okx_interval = timeframe_map.get(interval, '1H')
        # 优先尝试实时K线
        klines = market_client.get_candlesticks(instId=symbol, bar=okx_interval, limit=str(limit))
        if not klines.get('data'):
            logging.warning(f"未收到实时K线数据（{symbol}, {okx_interval}），尝试获取历史K线")
            klines = market_client.get_history_candlesticks(instId=symbol, bar=okx_interval, limit=str(limit))
        if not klines.get('data'):
            logging.warning(f"未收到K线数据（{symbol}, {okx_interval}）")
            return None
        # 记录原始响应
        logging.debug(f"K线原始数据: {klines['data'][:2]}...（共{len(klines['data'])}条）")
        df = pd.DataFrame(klines['data'], columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote', 'confirm'
        ])
        # 按时间戳升序排序
        df['timestamp'] = pd.to_numeric(df['timestamp'], errors='coerce')
        df = df.sort_values(by='timestamp', ascending=True).reset_index(drop=True)
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        # 仅保留已确认的 K 线
        df = df[df['confirm'] == '1']
        if df.empty:
            logging.warning(f"无已确认的K线数据（{symbol}, {okx_interval}）")
            return None
        df['close'] = pd.to_numeric(df['close'], errors='coerce')
        if df['close'].isna().any():
            logging.warning(f"K线数据中存在空值（{symbol}, {okx_interval}），已尝试填补")
            df['close'] = df['close'].fillna(method='ffill')
        # 验证最新 K 线时间
        server_time = get_server_time()
        latest_kline_time = df['timestamp'].iloc[-1]
        earliest_kline_time = df['timestamp'].iloc[0]
        time_diff = (server_time - latest_kline_time).total_seconds()
        logging.info(f"K线数据: {len(df)} 条, 最早时间: {earliest_kline_time}, 最新时间: {latest_kline_time}, 服务器时间: {server_time}")
        if time_diff > 172800:  # 放宽到48小时
            logging.warning(f"K线数据过旧，最新时间: {latest_kline_time}, 服务器时间: {server_time}")
            return None
        logging.info(f"获取K线数据: {len(df)} 条, 最新K线时间: {latest_kline_time}, 收盘价: {df['close'].iloc[-1]:.2f}")
        return df
    except Exception as e:
        update_queue.put(("status_label", f"获取K线失败: {str(e)}"))
        logging.error(f"获取K线失败（{symbol}, {okx_interval}）: {str(e)}\n{traceback.format_exc()}")
        return None

def calculate_rsi(df, period=14):
    try:
        if len(df) < period + 1:
            logging.warning(f"RSI 数据不足: {len(df)} 条")
            return None
        close = df['close']
        if close.isna().any():
            logging.warning("收盘价包含空值，RSI 计算可能不准确")
            return None
        rsi = talib.RSI(close, timeperiod=period)
        latest_rsi = rsi.iloc[-1]
        if pd.isna(latest_rsi):
            logging.warning("RSI 计算结果无效")
            return None
        logging.info(f"RSI 计算: 周期={period}, 最新RSI={latest_rsi:.2f}, 收盘价样本={close.tail(5).values}")
        return rsi
    except Exception as e:
        logging.error(f"RSI 计算错误: {str(e)}\n{traceback.format_exc()}")
        return None

def calculate_macd(df, fast=12, slow=26, signal=9):
    try:
        if len(df) < slow + signal - 1:
            logging.warning(f"MACD 数据不足: {len(df)} 条")
            return None, None, None
        close = df['close']
        macd, signal_line, histogram = talib.MACD(close, fastperiod=fast, slowperiod=slow, signalperiod=signal)
        logging.info(f"MACD 计算: MACD={macd.iloc[-1]:.2f}, 信号线={signal_line.iloc[-1]:.2f}, 柱状图={histogram.iloc[-1]:.2f}")
        return macd, signal_line, histogram
    except Exception as e:
        logging.error(f"MACD 计算错误: {str(e)}\n{traceback.format_exc()}")
        return None, None, None

def get_symbol_info(symbol='BTC-USDT'):
    rate_limit()
    try:
        info = public_client.get_instruments(instType='SPOT', instId=symbol)
        if info.get('code') != '0':
            raise Exception(f"获取交易对信息失败: {info.get('msg', '未知错误')}")
        quantity_precision = int(-np.log10(float(info['data'][0]['lotSz'])))
        min_qty = float(info['data'][0]['minSz'])
        return quantity_precision, min_qty
    except Exception as e:
        update_queue.put(("status_label", f"获取交易对信息失败: {str(e)}"))
        logging.error(f"获取交易对信息失败: {str(e)}")
        return 8, 0.0001

def place_order(side, quantity):
    try:
        quantity_precision, min_qty = get_symbol_info('BTC-USDT')
        quantity = round(quantity, quantity_precision)
        if quantity < min_qty:
            update_queue.put(("status_label", f"下单失败: 数量 {quantity} 小于最小值 {min_qty}"))
            logging.warning(f"下单失败: 数量 {quantity} 小于最小值 {min_qty}")
            return None
        params = {
            'instId': 'BTC-USDT',
            'tdMode': 'cash',
            'side': side.lower(),
            'ordType': 'market',
            'sz': str(quantity),
            'clOrdId': f"order_{int(time.time())}"
        }
        order = trade_client.place_order(**params)
        if order['code'] == '0':
            update_queue.put(("status_label", f"{side.upper()} 订单已下: 数量 {quantity:.6f} BTC"))
            logging.info(f"{side.upper()} 订单已下: {quantity:.6f} BTC")
            return order['data'][0]['ordId']
        else:
            update_queue.put(("status_label", f"下单失败: {order['msg']}"))
            logging.error(f"下单失败: {order['msg']}")
            return None
    except Exception as e:
        update_queue.put(("status_label", f"下单失败: {str(e)}"))
        logging.error(f"下单失败: {str(e)}")
        return None

def get_balance():
    rate_limit()
    try:
        balance = account_client.get_account_balance()
        if balance.get('code') != '0':
            raise Exception(f"获取余额失败: {balance.get('msg', '未知错误')}")
        usdt = float(
            next((asset for asset in balance['data'][0]['details'] if asset['ccy'] == 'USDT'), {'availBal': '0'})['availBal'])
        btc = float(
            next((asset for asset in balance['data'][0]['details'] if asset['ccy'] == 'BTC'), {'availBal': '0'})['availBal'])
        return usdt, btc
    except Exception as e:
        update_queue.put(("status_label", f"获取余额失败: {str(e)}"))
        logging.error(f"获取余额失败: {str(e)}")
        return None, None

def rsi_trading():
    global last_rsi_buy_time, last_rsi_sell_time, latest_rsi
    if not rsi_enabled:
        return
    try:
        df = get_klines('BTC-USDT', rsi_timeframe)
        if df is None:
            logging.warning("RSI 交易跳过: 无K线数据")
            return
        rsi = calculate_rsi(df)
        if rsi is None:
            logging.warning("RSI 交易跳过: RSI 计算失败")
            return
        latest_rsi_value = rsi.iloc[-1]
        if pd.isna(latest_rsi_value):
            logging.warning("RSI 交易跳过: 无效 RSI 值")
            return
        with lock:
            latest_rsi = latest_rsi_value
        usdt_balance, btc_balance = get_balance()
        if usdt_balance is None or btc_balance is None:
            logging.warning("RSI 交易跳过: 获取余额失败")
            return
        with lock:
            if current_price <= 0:
                logging.warning("RSI 交易跳过: 无效价格")
                return
            timeframe_to_cooldown = {
                '1m': timedelta(minutes=5),
                '5m': timedelta(minutes=15),
                '15m': timedelta(minutes=30),
                '30m': timedelta(minutes=45),
                '1h': timedelta(hours=1),
                '4h': timedelta(hours=4),
                '1d': timedelta(days=1)
            }
            cooldown = timeframe_to_cooldown.get(rsi_timeframe, timedelta(hours=1))
            now = datetime.now()
            if latest_rsi <= rsi_buy_value and (last_rsi_buy_time is None or (now - last_rsi_buy_time) >= cooldown):
                quantity = (usdt_balance * rsi_buy_ratio / 100) / current_price
                if quantity <= 0:
                    update_queue.put(("status_label", "RSI 买入失败: 余额不足"))
                    logging.warning("RSI 买入失败: 余额不足")
                    return
                order = place_order('buy', quantity)
                if order:
                    with lock:
                        last_rsi_buy_time = now
                    update_queue.put(("status_label", f"RSI 买入成功: 数量 {quantity:.6f} BTC"))
            if latest_rsi >= rsi_sell_value and btc_balance > 0 and (last_rsi_sell_time is None or (now - last_rsi_sell_time) >= cooldown):
                order = place_order('sell', btc_balance)
                if order:
                    with lock:
                        last_rsi_sell_time = now
                    update_queue.put(("status_label", f"RSI 卖出成功: 数量 {btc_balance:.6f} BTC"))
    except Exception as e:
        logging.error(f"RSI 交易错误: {str(e)}\n{traceback.format_exc()}")
        update_queue.put(("status_label", f"RSI 交易错误: {str(e)}"))

def macd_trading():
    global last_macd_buy_time, last_macd_sell_time, latest_macd, latest_signal, latest_histogram
    if not macd_enabled:
        return
    try:
        df = get_klines('BTC-USDT', macd_timeframe)
        if df is None:
            logging.warning("MACD 交易跳过: 无K线数据")
            return
        macd, signal_line, histogram = calculate_macd(df)
        if macd is None or signal_line is None or histogram is None:
            logging.warning("MACD 交易跳过: MACD 计算失败")
            return
        latest_macd_value = macd.iloc[-1]
        latest_signal_value = signal_line.iloc[-1]
        latest_histogram_value = histogram.iloc[-1]
        prev_macd = macd.iloc[-2]
        prev_signal = signal_line.iloc[-2]
        if any(pd.isna(x) for x in [latest_macd_value, prev_macd, latest_signal_value, prev_signal]):
            logging.warning("MACD 交易跳过: 无效 MACD 值")
            return
        with lock:
            latest_macd = latest_macd_value
            latest_signal = latest_signal_value
            latest_histogram = latest_histogram_value
        usdt_balance, btc_balance = get_balance()
        if usdt_balance is None or btc_balance is None:
            logging.warning("MACD 交易跳过: 获取余额失败")
            return
        with lock:
            if current_price <= 0:
                logging.warning("MACD 交易跳过: 无效价格")
                return
            timeframe_to_cooldown = {
                '1m': timedelta(minutes=5),
                '5m': timedelta(minutes=15),
                '15m': timedelta(minutes=30),
                '30m': timedelta(minutes=45),
                '1h': timedelta(hours=1),
                '4h': timedelta(hours=4),
                '1d': timedelta(days=1)
            }
            cooldown = timeframe_to_cooldown.get(macd_timeframe, timedelta(hours=1))
            now = datetime.now()
            if latest_macd < 0 and prev_macd < prev_signal and latest_macd > latest_signal and latest_histogram > 0 and (last_macd_buy_time is None or (now - last_macd_buy_time) >= cooldown):
                quantity = (usdt_balance * macd_buy_ratio / 100) / current_price
                if quantity <= 0:
                    update_queue.put(("status_label", "MACD 买入失败: 余额不足"))
                    logging.warning("MACD 买入失败: 余额不足")
                    return
                order = place_order('buy', quantity)
                if order:
                    with lock:
                        last_macd_buy_time = now
                    update_queue.put(("status_label", f"MACD 买入成功: 数量 {quantity:.6f} BTC"))
            if latest_macd > 0 and prev_macd > prev_signal and latest_macd < latest_signal and latest_histogram < 0 and btc_balance > 0 and (last_macd_sell_time is None or (now - last_macd_sell_time) >= cooldown):
                order = place_order('sell', btc_balance)
                if order:
                    with lock:
                        last_macd_sell_time = now
                    update_queue.put(("status_label", f"MACD 卖出成功: 数量 {btc_balance:.6f} BTC"))
    except Exception as e:
        logging.error(f"MACD 交易错误: {str(e)}\n{traceback.format_exc()}")
        update_queue.put(("status_label", f"MACD 交易错误: {str(e)}"))

def trading_loop():
    global running, animation_frame
    # 默认循环间隔（秒），用于价格和余额更新
    base_interval_seconds = 2
    if rsi_timeframe in ['1m', '5m'] or macd_timeframe in ['1m', '5m']:
        base_interval_seconds = 1

    # 动态K线请求间隔（秒），基于时间周期
    timeframe_to_interval = {
        '1m': 30,      # 1分钟周期，每30秒请求一次
        '5m': 150,     # 5分钟周期，每2.5分钟请求
        '15m': 450,    # 15分钟周期，每7.5分钟请求
        '30m': 900,    # 30分钟周期，每15分钟请求
        '1h': 1800,    # 1小时周期，每30分钟请求
        '4h': 7200,    # 4小时周期，每2小时请求
        '1d': 43200    # 1天周期，每12小时请求
    }

    # 确定K线请求间隔（取RSI和MACD中最短的）
    rsi_interval = timeframe_to_interval.get(rsi_timeframe, 1800) if rsi_enabled else float('inf')
    macd_interval = timeframe_to_interval.get(macd_timeframe, 1800) if macd_enabled else float('inf')
    kline_interval_seconds = min(rsi_interval, macd_interval)
    logging.info(f"K线请求间隔: {kline_interval_seconds}秒 (RSI={rsi_timeframe}, MACD={macd_timeframe})")

    # 跟踪上次K线请求时间
    last_kline_request = 0

    while running:
        try:
            current_time = time.time()
            logging.debug(f"交易循环: RSI 启用={rsi_enabled}, MACD 启用={macd_enabled}, 基础间隔={base_interval_seconds}s")

            # 获取价格
            price = get_btc_price()
            if not price:
                with lock:
                    if price_update_time and (datetime.now() - price_update_time) > timedelta(minutes=5):
                        update_queue.put(("price_label", f"当前 BTC 价格: ${current_price:.2f} (数据可能已过期)", (255, 165, 0)))
                        logging.warning("价格数据过期")
                    else:
                        animation_frame += 1
                        color = (255, 255, 0) if animation_frame % 20 < 10 else (0, 255, 255)
                        update_queue.put(("price_label", f"当前 BTC 价格: ${current_price:.2f}", color))
                        logging.debug(f"价格更新（缓存）: ${current_price:.2f}")
                time.sleep(base_interval_seconds)
                continue

            animation_frame += 1
            color = (255, 255, 0) if animation_frame % 20 < 10 else (0, 255, 255)
            update_queue.put(("price_label", f"当前 BTC 价格: ${price:.2f}", color))
            logging.debug(f"价格更新（HTTP）: ${price:.2f}")

            # 获取余额
            usdt_balance, btc_balance = get_balance()
            if usdt_balance is not None and btc_balance is not None:
                update_queue.put(("balance_label", f"余额: {usdt_balance:.2f} USDT, {btc_balance:.6f} BTC", (255, 255, 255)))
                logging.debug(f"余额更新: {usdt_balance:.2f} USDT, {btc_balance:.6f} BTC")
            else:
                update_queue.put(("balance_label", "余额: 获取失败", (255, 0, 0)))
                logging.warning("余额获取失败")

            # 检查是否需要请求K线数据
            if current_time - last_kline_request >= kline_interval_seconds:
                logging.info(f"触发K线请求: 时间={datetime.now()}, 间隔={kline_interval_seconds}s")
                # 计算RSI
                if rsi_enabled:
                    df_rsi = get_klines('BTC-USDT', rsi_timeframe)
                    if df_rsi is not None:
                        rsi = calculate_rsi(df_rsi)
                        if rsi is not None and not pd.isna(rsi.iloc[-1]):
                            with lock:
                                global latest_rsi
                                latest_rsi = rsi.iloc[-1]
                                update_queue.put(("rsi_display", f"RSI(14): {latest_rsi:.2f}", (255, 255, 255)))
                                logging.debug(f"RSI 更新: {latest_rsi:.2f}")
                        else:
                            update_queue.put(("status_label", "RSI 计算失败"))
                            logging.warning("RSI 计算失败")
                    else:
                        update_queue.put(("status_label", "RSI 交易跳过：无法获取K线数据"))
                        logging.warning("RSI K线数据获取失败")

                # 计算MACD
                if macd_enabled:
                    df_macd = get_klines('BTC-USDT', macd_timeframe)
                    if df_macd is not None:
                        macd, signal_line, histogram = calculate_macd(df_macd)
                        if macd is not None and not pd.isna(macd.iloc[-1]):
                            with lock:
                                global latest_macd, latest_signal, latest_histogram
                                latest_macd = macd.iloc[-1]
                                latest_signal = signal_line.iloc[-1]
                                latest_histogram = histogram.iloc[-1]
                                update_queue.put(("macd_display", f"MACD(12,26,9): {latest_macd:.2f}", (255, 255, 255)))
                                update_queue.put(("signal_display", f"信号线(9): {latest_signal:.2f}", (255, 255, 255)))
                                update_queue.put(("histogram_display", f"柱状图: {latest_histogram:.2f}", (255, 255, 255)))
                                logging.debug(f"MACD 更新: MACD={latest_macd:.2f}, 信号线={latest_signal:.2f}, 柱状图={latest_histogram:.2f}")
                        else:
                            update_queue.put(("status_label", "MACD 计算失败"))
                            logging.warning("MACD 计算失败")
                    else:
                        update_queue.put(("status_label", "MACD 交易跳过：无法获取K线数据"))
                        logging.warning("MACD K线数据获取失败")

                last_kline_request = current_time
                logging.info("K线请求完成，等待下次请求")

            # 动态颜色
            t = (animation_frame % 60) / 60.0
            r = int(255 * (1 - t))
            g = int(255 * t)
            b = 255
            update_queue.put(("rsi_enabled", None, (r, g, b)))
            update_queue.put(("macd_enabled", None, (255 - r, 255 - g, b)))

            t_status = (animation_frame % 40) / 40.0
            r_status = int(255 * t_status)
            g_status = int(255 * (1 - t_status))
            b_status = 255
            update_queue.put(("status_label", None, (r_status, g_status, b_status)))

            rsi_trading()
            macd_trading()
            gc.collect()
        except Exception as e:
            logging.error(f"交易循环错误: {str(e)}\n{traceback.format_exc()}")
            update_queue.put(("status_label", f"交易错误: {str(e)}"))
        time.sleep(base_interval_seconds)

def update_ui_callback():
    global last_ui_log_time
    try:
        current_time = time.time()
        updates_processed = 0  # 跟踪处理的队列项数

        # 处理 update_queue 中的所有更新
        while not update_queue.empty():
            item = update_queue.get()
            tag = item[0]
            value = item[1]
            color = item[2] if len(item) > 2 else None
            if value is not None:
                dpg.set_value(tag, value)
                logging.debug(f"UI 更新值: {tag} = {value}")
            if color is not None:
                with dpg.theme() as temp_theme:
                    with dpg.theme_component(dpg.mvAll):
                        dpg.add_theme_color(dpg.mvThemeCol_Text, value=color)
                    dpg.bind_item_theme(tag, temp_theme)
                logging.debug(f"UI 更新颜色: {tag} = {color}")
            updates_processed += 1

        # 实时更新指标（即使队列为空，确保显示最新值）
        if running:
            with lock:
                # 更新 RSI 显示
                if latest_rsi is not None and not pd.isna(latest_rsi):
                    rsi_text = f"RSI(14): {latest_rsi:.2f}"
                    rsi_color = (255, 0, 0) if latest_rsi >= rsi_sell_value else (0, 255, 0) if latest_rsi <= rsi_buy_value else (255, 255, 255)
                    dpg.set_value("rsi_display", rsi_text)
                    with dpg.theme() as rsi_theme:
                        with dpg.theme_component(dpg.mvAll):
                            dpg.add_theme_color(dpg.mvThemeCol_Text, value=rsi_color)
                        dpg.bind_item_theme("rsi_display", rsi_theme)
                    logging.debug(f"UI 更新 RSI: {rsi_text}, 颜色={rsi_color}")
                else:
                    dpg.set_value("rsi_display", "RSI(14): 无数据")
                    with dpg.theme() as rsi_theme:
                        with dpg.theme_component(dpg.mvAll):
                            dpg.add_theme_color(dpg.mvThemeCol_Text, value=(255, 255, 255))
                        dpg.bind_item_theme("rsi_display", rsi_theme)
                    logging.debug("UI 更新 RSI: 无数据")

                # 更新 MACD 显示
                if latest_macd is not None and not pd.isna(latest_macd):
                    macd_text = f"MACD(12,26,9): {latest_macd:.2f}"
                    macd_color = (0, 255, 0) if latest_macd > 0 else (255, 0, 0) if latest_macd < 0 else (255, 255, 255)
                    dpg.set_value("macd_display", macd_text)
                    with dpg.theme() as macd_theme:
                        with dpg.theme_component(dpg.mvAll):
                            dpg.add_theme_color(dpg.mvThemeCol_Text, value=macd_color)
                        dpg.bind_item_theme("macd_display", macd_theme)
                    logging.debug(f"UI 更新 MACD: {macd_text}, 颜色={macd_color}")
                else:
                    dpg.set_value("macd_display", "MACD(12,26,9): 无数据")
                    with dpg.theme() as macd_theme:
                        with dpg.theme_component(dpg.mvAll):
                            dpg.add_theme_color(dpg.mvThemeCol_Text, value=(255, 255, 255))
                        dpg.bind_item_theme("macd_display", macd_theme)
                    logging.debug("UI 更新 MACD: 无数据")

                # 更新信号线显示
                if latest_signal is not None and not pd.isna(latest_signal):
                    signal_text = f"信号线(9): {latest_signal:.2f}"
                    signal_color = (255, 255, 0)  # 固定黄色
                    dpg.set_value("signal_display", signal_text)
                    with dpg.theme() as signal_theme:
                        with dpg.theme_component(dpg.mvAll):
                            dpg.add_theme_color(dpg.mvThemeCol_Text, value=signal_color)
                        dpg.bind_item_theme("signal_display", signal_theme)
                    logging.debug(f"UI 更新 信号线: {signal_text}, 颜色={signal_color}")
                else:
                    dpg.set_value("signal_display", "信号线(9): 无数据")
                    with dpg.theme() as signal_theme:
                        with dpg.theme_component(dpg.mvAll):
                            dpg.add_theme_color(dpg.mvThemeCol_Text, value=(255, 255, 255))
                        dpg.bind_item_theme("signal_display", signal_theme)
                    logging.debug("UI 更新 信号线: 无数据")

                # 更新柱状图显示
                if latest_histogram is not None and not pd.isna(latest_histogram):
                    hist_text = f"柱状图: {latest_histogram:.2f}"
                    hist_color = (0, 255, 0) if latest_histogram > 0 else (255, 0, 0) if latest_histogram < 0 else (255, 255, 255)
                    dpg.set_value("histogram_display", hist_text)
                    with dpg.theme() as hist_theme:
                        with dpg.theme_component(dpg.mvAll):
                            dpg.add_theme_color(dpg.mvThemeCol_Text, value=hist_color)
                        dpg.bind_item_theme("histogram_display", hist_theme)
                    logging.debug(f"UI 更新 柱状图: {hist_text}, 颜色={hist_color}")
                else:
                    dpg.set_value("histogram_display", "柱状图: 无数据")
                    with dpg.theme() as hist_theme:
                        with dpg.theme_component(dpg.mvAll):
                            dpg.add_theme_color(dpg.mvThemeCol_Text, value=(255, 255, 255))
                        dpg.bind_item_theme("histogram_display", hist_theme)
                    logging.debug("UI 更新 柱状图: 无数据")

        # 定期记录 UI 刷新状态
        if current_time - last_ui_log_time >= 5:  # 每5秒记录一次
            logging.info(f"UI 刷新触发，处理队列更新数={updates_processed}")
            last_ui_log_time = current_time

    except Exception as e:
        logging.error(f"UI 更新错误: {str(e)}\n{traceback.format_exc()}")
        # 推送错误到状态标签
        try:
            dpg.set_value("status_label", f"UI 更新错误: {str(e)}")
            with dpg.theme() as error_theme:
                with dpg.theme_component(dpg.mvAll):
                    dpg.add_theme_color(dpg.mvThemeCol_Text, value=(255, 0, 0))
                dpg.bind_item_theme("status_label", error_theme)
        except:
            pass  # 防止状态标签更新失败导致崩溃

def start_trading():
    global running
    try:
        if not trade_client:
            update_queue.put(("status_label", "请输入有效的 API 密钥"))
            logging.warning("开始交易失败: 无交易客户端")
            return
        if not ws_connected:
            update_queue.put(("status_label", "WebSocket 正在重连"))
            config = load_config()
            threading.Thread(target=start_websocket,
                            args=(config.get('api_key', ''), config.get('api_secret', ''), config.get('passphrase', '')),
                            daemon=True).start()
            time.sleep(10)
            if not ws_connected:
                update_queue.put(("status_label", "WebSocket 连接失败"))
                logging.error("WebSocket 连接失败")
                return
        running = True
        threading.Thread(target=trading_loop, daemon=True).start()
        update_queue.put(("status_label", "交易已启动"))
        logging.info("交易已启动")
    except Exception as e:
        logging.error(f"开始交易错误: {str(e)}\n{traceback.format_exc()}")
        update_queue.put(("status_label", f"交易启动失败: {str(e)}"))

def stop_trading():
    global running
    try:
        running = False
        update_queue.put(("status_label", "交易已停止"))
        logging.info("交易已停止")
    except Exception as e:
        logging.error(f"停止交易错误: {str(e)}\n{traceback.format_exc()}")
        update_queue.put(("status_label", f"交易停止失败: {str(e)}"))

def save_settings():
    global rsi_enabled, macd_enabled, rsi_buy_ratio, macd_buy_ratio, rsi_timeframe, macd_timeframe, rsi_buy_value, rsi_sell_value
    try:
        rsi_enabled = dpg.get_value("rsi_enabled")
        macd_enabled = dpg.get_value("macd_enabled")
        rsi_buy_ratio = dpg.get_value("rsi_buy_ratio")
        macd_buy_ratio = dpg.get_value("macd_buy_ratio")
        rsi_timeframe = timeframe_display_map.get(dpg.get_value("rsi_timeframe"), '1h')
        macd_timeframe = timeframe_display_map.get(dpg.get_value("macd_timeframe"), '1h')
        rsi_buy_value = dpg.get_value("rsi_buy_value")
        rsi_sell_value = dpg.get_value("rsi_sell_value")
        update_queue.put(("status_label", "设置已保存"))
        logging.info("设置已保存")
    except Exception as e:
        logging.error(f"保存设置错误: {str(e)}\n{traceback.format_exc()}")
        update_queue.put(("status_label", f"设置保存失败: {str(e)}"))

def save_api():
    try:
        api_key = dpg.get_value("api_key")
        api_secret = dpg.get_value("api_secret")
        passphrase = dpg.get_value("passphrase")
        for flag in ['0', '1']:
            if init_okx(api_key, api_secret, passphrase, flag=flag):
                save_config(api_key, api_secret, passphrase)
                threading.Thread(target=start_websocket,
                                args=(api_key, api_secret, passphrase),
                                daemon=True).start()
                timeout = 10
                start_time = time.time()
                while time.time() - start_time < timeout and not ws_connected:
                    time.sleep(1)
                if ws_connected:
                    update_queue.put(("status_label", f"API 已保存，WebSocket 已连接 (flag={flag})"))
                    logging.info(f"API 已保存，WebSocket 已连接 (flag={flag})")
                    usdt, btc = get_balance()
                    if usdt is not None and btc is not None:
                        update_queue.put(("balance_label", f"余额: {usdt:.2f} USDT, {btc:.6f} BTC", (255, 255, 255)))
                    else:
                        update_queue.put(("balance_label", "余额: 获取失败", (255, 0, 0)))
                    return True
                else:
                    update_queue.put(("status_label", f"API 已保存，WebSocket 连接失败 (flag={flag})"))
                    logging.warning(f"WebSocket 连接失败 (flag={flag})")
            update_queue.put(("status_label", f"无效的 API 密钥 (flag={flag})"))
            logging.error(f"无效的 API 密钥 (flag={flag})")
        return False
    except Exception as e:
        logging.error(f"保存 API 错误- {str(e)}\n{traceback.format_exc()}")
        update_queue.put(("status_label", f"API 保存失败: {str(e)}"))
        return False

def button_animation(sender):
    original_color = (255, 69, 0, 255)
    highlight_color = (255, 165, 0, 255)
    with dpg.theme() as temp_theme:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, highlight_color)
        dpg.bind_item_theme(sender, temp_theme)
    time.sleep(0.1)
    with dpg.theme() as restore_theme:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, original_color)
        dpg.bind_item_theme(sender, restore_theme)

def show_help_window():
    if dpg.does_item_exist("help_window"):
        dpg.delete_item("help_window")

    with dpg.window(label="使用说明书", tag="help_window", width=700, height=400, pos=(100, 100), no_scrollbar=False):
        dpg.add_text("BTC 金丝木量化交易机器人 - 使用说明书", color=(0, 255, 255))
        dpg.add_separator()

        dpg.add_text("1. 交易逻辑", color=(255, 215, 0))
        dpg.add_text("本程序基于 RSI 和 MACD 指标进行自动化交易，适用于 OKX 平台的 BTC/USDT 现货交易")
        dpg.add_text("- RSI 交易逻辑：")
        dpg.add_text("  * 买入：当 RSI(14) ≤ 买入阈值（默认 30），且距离上次买入超过冷却时间")
        dpg.add_text("  * 卖出：当 RSI(14) ≥ 卖出阈值（默认 70），且距离上次卖出超过冷却时间")
        dpg.add_text("- MACD 交易逻辑：")
        dpg.add_text("  * 买入：当 MACD 线 < 0，MACD 线从下方穿过信号线（金叉），且柱状图 > 0")
        dpg.add_text("  * 卖出：当 MACD 线 > 0，MACD 线从上方穿过信号线（死叉），且柱状图 < 0")
        dpg.add_separator()

        dpg.add_text("2. 使用方式", color=(255, 215, 0))
        dpg.add_text("步骤：")
        dpg.add_text("1) 在 'OKX API 设置' 中输入你的 API Key、API Secret 和 Passphrase，点击 '保存 API 密钥'。")
        dpg.add_text("   - 确保 API 密钥具有交易和余额查询权限")
        dpg.add_text("2) 在 'RSI 交易设置' 和 'MACD 交易设置' 中配置参数-")
        dpg.add_text("   - 勾选 '启用 RSI 交易' 或 '启用 MACD 交易' 以激活相应策略")
        dpg.add_text("   - 设置单次买入比例（%）：决定每次买入时使用多少 USDT 余额")
        dpg.add_text("   - 选择时间周期：决定指标计算的 K 线周期 -如 1分钟 1小时 1日")
        dpg.add_text("   注，时间周期也会影响到你的K线获取频率的，频率通常为时间周期的一半")
        dpg.add_text("   - 设置 RSI 买入/卖出阈值：调整 RSI 交易的触发条件")
        dpg.add_text("3) 点击 '保存设置' 确认参数！（必须操作）")
        dpg.add_text("4) 点击 '启动交易' 开始自动化交易")
        dpg.add_text("5) 点击 '停止交易' 暂停交易")
        dpg.add_text("6) 查看 '指标实时监控' 部分，了解当前的 RSI 和 MACD 指标值")
        dpg.add_text("注意事项-")
        dpg.add_text("- 确保账户有足够的 USDT 和 BTC 余额以执行交易")
        dpg.add_text("- 交易日志保存在 'websocket.log'，可用于调试")
        dpg.add_text("- WebSocket 提供实时价格更新，若断开会自动重连")
        dpg.add_separator()

        dpg.add_text("3. 动态冷却时间", color=(255, 215, 0))
        dpg.add_text("为避免过于频繁的交易，程序为 RSI 和 MACD 交易设置了动态冷却时间")
        dpg.add_text("- 1分钟-冷却时间 5 分钟")
        dpg.add_text("- 5分钟-冷却时间 15 分钟")
        dpg.add_text("- 15分钟-冷却时间 30 分钟")
        dpg.add_text("- 30分钟-冷却时间 45 分钟")
        dpg.add_text("- 1小时-冷却时间 1 小时")
        dpg.add_text("- 4小时-冷却时间 4 小时")
        dpg.add_text("- 1日-冷却时间 1 天")
        dpg.add_text("作用-")
        dpg.add_text("- 冷却时间根据选择的时间周期自动调整，确保交易频率合理")
        dpg.add_text("- 短周期适合短期交易-长周期适合趋势交易")
        dpg.add_separator()

        dpg.add_text("想支持树酱的话可以向以下钱包地址捐款-", color=(128, 128, 128))
        dpg.add_text("0x4FdFCfc03A5416EB5D9B85F4bad282c0DaF19783", color=(128, 128, 128))
        dpg.add_text("感谢你的支持呀 -不捐也没关系，作者会自己找垃圾吃的-", color=(128, 128, 128))

def create_gui():
    global last_ui_log_time
    logging.info("创建 DearPyGui 上下文")
    try:
        dpg.create_context()
        icon_path = os.path.join(os.path.dirname(__file__), "jio.ico")
        if not os.path.exists(icon_path):
            logging.warning(f"图标文件未找到: {icon_path}")
        dpg.create_viewport(
            title='Tree Bot',
            width=720,
            height=920,
            small_icon=icon_path if os.path.exists(icon_path) else "",
            large_icon=icon_path if os.path.exists(icon_path) else ""
        )

        font_path = os.path.join(os.path.dirname(__file__), "NotoSerifCJKsc-dick.otf")
        if not os.path.exists(font_path):
            logging.error(f"字体文件未找到: {font_path}")
            font_path = r"C:\Windows\Fonts\simhei.ttf"
            if not os.path.exists(font_path):
                logging.error(f"备用字体失败: {font_path}")
                return

        with dpg.font_registry():
            with dpg.font(font_path, size=28) as title_font:
                dpg.add_font_range(first_char=0x4E00, last_char=0x9FFF)
            with dpg.font(font_path, size=20) as body_font:
                dpg.add_font_range(first_char=0x4E00, last_char=0x9FFF)
            dpg.bind_font(body_font)

        with dpg.theme() as global_theme:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, value=(30, 30, 50, 255))
                dpg.add_theme_color(dpg.mvThemeCol_Text, value=(255, 255, 255, 255))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, value=(50, 50, 70, 255))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, value=(70, 70, 90, 255))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, value=(90, 90, 110, 255))
                dpg.add_theme_color(dpg.mvThemeCol_CheckMark, value=(0, 255, 0, 255))
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrab, value=(255, 215, 0, 255))
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrabActive, value=(255, 255, 0, 255))

        with dpg.theme() as button_theme:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, value=(255, 69, 0, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, value=(255, 99, 71, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, value=(255, 140, 0, 255))
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 10)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 10, 5)

        with dpg.theme() as section_theme:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_Text, value=(0, 255, 255, 255))

        dpg.bind_theme(global_theme)

        with dpg.window(label="树酱提示 本软件完全免费开源 你从任何途径购买都说明被骗了", width=800, height=900, pos=(0, 0), no_scrollbar=True):
            with dpg.group():
                title_text = "BTC 金丝木量化交易 -OKX 现货"
                title_width = len(title_text) * 18
                with dpg.drawlist(width=title_width, height=40):
                    for i in range(title_width):
                        t = i / title_width
                        r = int(255 * (1 - t))
                        g = int(215 + 40 * t)
                        b = int(0 + 255 * t)
                        dpg.draw_line(p1=(i, 0), p2=(i, 40), color=(r, g, b, 255))
                    dpg.draw_text(pos=(0, 0), text=title_text, size=28)
                dpg.bind_item_font(item=dpg.last_item(), font=title_font)
                dpg.add_separator()

                dpg.add_text("OKX API 设置")
                dpg.bind_item_theme(item=dpg.last_item(), theme=section_theme)
                with dpg.group(horizontal=True):
                    dpg.add_input_text(label="API Key", tag="api_key", default_value=load_config().get('api_key', ''), width=300)  # 修正 width 值
                with dpg.group(horizontal=True):
                    dpg.add_input_text(label="API Secret", tag="api_secret", default_value=load_config().get('api_secret', ''), password=True, width=300)  # 修正 width 值
                with dpg.group(horizontal=True):
                    dpg.add_input_text(label="Passphrase", tag="passphrase", default_value=load_config().get('passphrase', ''), password=True, width=300)  # 修正 width 值
                dpg.add_button(label="保存 API 密钥", callback=lambda: (
                    save_api(), threading.Thread(target=lambda: button_animation(dpg.last_item()), daemon=True).start()))
                dpg.bind_item_theme(item=dpg.last_item(), theme=button_theme)

            dpg.add_separator()

            with dpg.group():
                dpg.add_text("当前 BTC 价格: $0.00", tag="price_label")
                dpg.bind_item_font("price_label", title_font)
                dpg.add_text("余额: 0.00 USDT, 0.000000 BTC", tag="balance_label")
                dpg.bind_item_font("balance_label", body_font)

            dpg.add_separator()

            with dpg.group():
                dpg.add_text("指标实时监控", color=(0, 255, 255))
                dpg.bind_item_font(dpg.last_item(), title_font)
                dpg.add_separator()
                dpg.add_text("RSI(14): 无数据", tag="rsi_display")
                dpg.bind_item_font("rsi_display", body_font)
                dpg.add_text("MACD(12,26,9): 无数据", tag="macd_display")
                dpg.bind_item_font("macd_display", body_font)
                dpg.add_text("信号线(9): 无数据", tag="signal_display")
                dpg.bind_item_font("signal_display", body_font)
                dpg.add_text("柱状图: 无数据", tag="histogram_display")
                dpg.bind_item_font("histogram_display", body_font)

            dpg.add_separator()

            with dpg.group():
                dpg.add_text("RSI 交易设置")
                dpg.bind_item_theme(dpg.last_item(), section_theme)
                dpg.add_checkbox(label="启用 RSI 交易", tag="rsi_enabled")
                with dpg.group(horizontal=True):
                    dpg.add_input_float(label="单次买入比例 (%)", tag="rsi_buy_ratio", default_value=10.0,
                                        min_value=0.0, max_value=100.0, width=200)
                with dpg.group(horizontal=True):
                    dpg.add_combo(label="RSI 时间周期", tag="rsi_timeframe",
                                  items=list(timeframe_display_map.keys()), default_value="1小时", width=200)
                with dpg.group(horizontal=True):
                    dpg.add_input_float(label="买入 RSI 值", tag="rsi_buy_value", default_value=30.0,
                                        min_value=0.0, max_value=100.0, width=200)
                with dpg.group(horizontal=True):
                    dpg.add_input_float(label="卖出 RSI 值", tag="rsi_sell_value", default_value=70.0,
                                        min_value=0.0, max_value=100.0, width=200)

            dpg.add_separator()

            with dpg.group():
                dpg.add_text("MACD 交易设置")
                dpg.bind_item_theme(dpg.last_item(), section_theme)
                dpg.add_checkbox(label="启用 MACD 交易", tag="macd_enabled")
                with dpg.group(horizontal=True):
                    dpg.add_input_float(label="单次买入比例 (%)", tag="macd_buy_ratio", default_value=10.0,
                                        min_value=0.0, max_value=100.0, width=200)
                with dpg.group(horizontal=True):
                    dpg.add_combo(label="MACD 时间周期", tag="macd_timeframe",
                                  items=list(timeframe_display_map.keys()), default_value="1小时", width=200)

            dpg.add_separator()

            with dpg.group(horizontal=True):
                dpg.add_button(label="保存设置", callback=lambda: (
                    save_settings(), threading.Thread(target=lambda: button_animation(dpg.last_item()), daemon=True).start()))
                dpg.bind_item_theme(dpg.last_item(), button_theme)
                dpg.add_button(label="启动交易", callback=lambda: (
                    start_trading(), threading.Thread(target=lambda: button_animation(dpg.last_item()), daemon=True).start()))
                dpg.bind_item_theme(dpg.last_item(), button_theme)
                dpg.add_button(label="停止交易", callback=lambda: (
                    stop_trading(), threading.Thread(target=lambda: button_animation(dpg.last_item()), daemon=True).start()))
                dpg.bind_item_theme(dpg.last_item(), button_theme)

            dpg.add_separator()

            with dpg.group():
                dpg.add_text("状态: 未启动", tag="status_label")
                dpg.bind_item_font(dpg.last_item(), title_font)

            dpg.add_button(label="？", callback=lambda: (
                show_help_window(), threading.Thread(target=lambda: button_animation(dpg.last_item()), daemon=True).start()),
                           pos=(600, 720))
            dpg.bind_item_theme(dpg.last_item(), button_theme)

        dpg.setup_dearpygui()
        dpg.show_viewport()
        logging.info("DearPyGui 视窗已显示")
        while dpg.is_dearpygui_running():
            update_ui_callback()
            dpg.render_dearpygui_frame()
        dpg.destroy_context()
        logging.info("DearPyGui 上下文已销毁")
    except Exception as e:
        logging.error(f"GUI 创建失败: {str(e)}\n{traceback.format_exc()}")
        raise

def main():
    logging.info("启动主函数")
    config = load_config()
    if config and config.get('api_key') and config.get('api_secret') and config.get('passphrase'):
        try:
            for flag in ['0', '1']:
                if init_okx(config.get('api_key'), config.get('api_secret', ''), config.get('passphrase', ''), flag=flag):
                    threading.Thread(
                        target=start_websocket,
                        args=(config.get('api_key'), config.get('api_secret', ''), config.get('passphrase', '')),
                        daemon=True
                    ).start()
                    time.sleep(10)
                    usdt, btc = get_balance()
                    if usdt is not None and btc is not None:
                        update_queue.put(("balance_label", f"余额: {usdt:.2f} USDT, {btc:.6f} BTC", (255, 255, 255)))
                    else:
                        update_queue.put(("balance_label", "余额: 获取失败", (255, 0, 0)))
                    break
            else:
                logging.error("初始化 OKX API 失败")
                update_queue.put(("status_label", "初始化 OKX API 失败"))
        except Exception as e:
            logging.error(f"初始化错误: {str(e)}\n{traceback.format_exc()}")
            update_queue.put(("status_label", f"初始化失败: {str(e)}"))
    create_gui()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.error(f"主程序错误: {str(e)}\n{traceback.format_exc()}")
        raise