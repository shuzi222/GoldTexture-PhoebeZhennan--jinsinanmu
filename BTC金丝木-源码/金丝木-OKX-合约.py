import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import json
import time
import threading
import asyncio
from datetime import datetime, timedelta
from queue import Queue
import gc
import numpy as np
import pandas as pd
import talib
import dearpygui.dearpygui as dpg
import okx.Trade as Trade
import okx.MarketData as MarketData
import okx.Account as Account
import okx.PublicData as PublicData
from okx.websocket.WsPrivateAsync import WsPrivateAsync
import traceback

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
rsi_leverage = 3.0
macd_leverage = 3.0
rsi_margin_mode = 'cross'
macd_margin_mode = 'cross'
rsi_take_profit = 10.0
rsi_stop_loss = 5.0
macd_take_profit = 10.0
macd_stop_loss = 5.0
rsi_max_position_ratio = 50.0
macd_max_position_ratio = 50.0
rsi_trade_direction = 'both'
macd_trade_direction = 'both'
rsi_cooldown_multiplier = 1.0
macd_cooldown_multiplier = 1.0
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
position_updates = Queue()
loop = None
last_message_time = None
last_ui_log_time = 0
last_ws_log_time = 0
request_timestamps = [] # 这三个
REQUEST_LIMIT = 3       # 和旧版代码有关
REQUEST_WINDOW = 2.0    # 懒得删了
rsi_tp_sl_mode = "exchange"  # RSI 止盈止损方式
macd_tp_sl_mode = "exchange"  # MACD 止盈止损方式
last_request_time = 0.0  # 上次 API 请求时间
request_count = 0  # API 请求计数
websocket_thread_running = False

# 时间框架映射
timeframe_display_map = {
    '1分钟': '1m', '5分钟': '5m', '15分钟': '15m', '30分钟': '30m',
    '1小时': '1H', '4小时': '4H', '1日': '1H'
}
timeframe_reverse_map = {v: k for k, v in timeframe_display_map.items()}

def rate_limit():
    global last_request_time, request_count
    current_time = time.time()
    with lock:
        if current_time - last_request_time > 2:
            request_count = 0
            last_request_time = current_time
        request_count += 1
        if request_count >= 18:  # 留 2 次余量
            sleep_time = 2.1 - (current_time - last_request_time)
            if sleep_time > 0:
                time.sleep(sleep_time)
            request_count = 0
            last_request_time = time.time()

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
        logging.error(f"保存配置文件失败: {str(e)}\n{traceback.format_exc()}")

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

def set_leverage(symbol, leverage, margin_mode):
    rate_limit()
    try:
        params = {
            'instId': symbol,
            'lever': str(leverage),
            'mgnMode': margin_mode
        }
        response = account_client.set_leverage(**params)
        if response.get('code') == '0':
            logging.info(f"设置杠杆倍数成功: {symbol}, 杠杆={leverage}, 模式={margin_mode}")
            return True
        else:
            raise Exception(f"设置杠杆失败: {response.get('msg', '未知错误')}")
    except Exception as e:
        update_queue.put(("status_label", f"设置杠杆失败: {str(e)}"))
        logging.error(f"设置杠杆失败: {str(e)}")
        return False

def ws_callback(message):
    global current_price, price_update_time, ws_connected, last_message_time, last_ws_log_time
    with lock:
        last_message_time = datetime.now()
        try:
            data = json.loads(message)
            current_time = time.time()
            if current_time - last_ws_log_time >= 2:
                logging.debug(f"WebSocket 消息: {data}")
                last_ws_log_time = current_time
            event = data.get('event')
            if event == 'login':
                if data.get('code') == '0':
                    ws_connected = True
                    # update_queue.put(("status_label", "WebSocket 已连接"))
                    logging.info(f"WebSocket 登录成功: {data}")
                else:
                    ws_connected = False
                    # update_queue.put(("status_label", f"WebSocket 登录失败: {data.get('msg', '')}"))
                    logging.error(f"WebSocket 登录失败: {data}")
            elif data.get('arg', {}).get('channel') == 'bbo-tbt':
                logging.info(f"收到 bbo-tbt 数据: {data}")
                try:
                    price = float(data['data'][0]['bids'][0][0])
                    with lock:
                        current_price = price
                        price_update_time = datetime.now()
                    update_queue.put(("price_label", f"当前 BTC 价格: ${price:.2f}", (255, 255, 255)))
                    logging.info(f"WebSocket 价格更新: ${price:.2f}")
                except (KeyError, IndexError, ValueError) as e:
                    logging.error(f"WebSocket 价格解析错误: {str(e)}, 数据: {data}")
            elif data.get('arg', {}).get('channel') in ['orders', 'positions']:
                logging.debug(f"收到 {data['arg']['channel']} 数据: {data}")
        except Exception as e:
            logging.error(f"WebSocket 回调错误: {str(e)}\n{traceback.format_exc()}")


async def init_websocket(api_key, api_secret, passphrase):
    global ws, ws_connected, last_message_time
    max_retries = 5  # 最大重试次数
    base_retry_delay = 5  # 基础重试间隔（秒）

    for attempt in range(max_retries):
        try:
            last_message_time = datetime.now()
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
                {"channel": "bbo-tbt", "instId": "BTC-USDT-SWAP"},
                {"channel": "orders", "instType": "SWAP", "instId": "BTC-USDT-SWAP"},
                {"channel": "positions", "instType": "SWAP", "instId": "BTC-USDT-SWAP"}
            ]
            await ws.subscribe(args, callback=ws_callback)
            logging.info("已订阅 bbo-tbt、orders 和 positions 频道")

            # 等待订阅确认
            start_time = time.time()
            while time.time() - start_time < 5 and not ws_connected:
                await asyncio.sleep(1)
            if not ws_connected:
                logging.warning("WebSocket 订阅未确认")
                raise Exception("订阅超时")

            # 监控连接状态
            while ws_connected:
                if last_message_time and (datetime.now() - last_message_time).total_seconds() > 60:
                    ws_connected = False
                    logging.warning("WebSocket 60秒无响应，断开连接")
                    break
                await asyncio.sleep(5)
        except Exception as e:
            ws_connected = False
            retry_delay = base_retry_delay * (2 ** attempt)  # 指数退避
            logging.error(f"WebSocket 尝试 {attempt + 1}/{max_retries} 失败: {str(e)}\n{traceback.format_exc()}")
            update_queue.put(("status_label", f"WebSocket 连接失败 (尝试 {attempt + 1}/{max_retries}): {str(e)}"))
            if attempt < max_retries - 1:
                logging.info(f"等待 {retry_delay} 秒后重试...")
                await asyncio.sleep(retry_delay)
            else:
                logging.error("WebSocket 重试次数耗尽")
                update_queue.put(("status_label", "WebSocket 连接失败，重试次数耗尽"))
                raise Exception("WebSocket 重试失败")
        finally:
            try:
                if ws and hasattr(ws, 'websocket') and ws.websocket:
                    await ws.websocket.close()
                    logging.info("WebSocket 已关闭")
            except Exception as e:
                logging.error(f"关闭 WebSocket 失败: {str(e)}")

    # 如果重试失败，设置标志以允许下次重试
    global websocket_thread_running
    websocket_thread_running = False


def start_websocket(api_key, api_secret, passphrase):
    global loop, websocket_thread_running
    # 检查是否已有WebSocket线程
    if websocket_thread_running or any(t.name == "websocket_thread" for t in threading.enumerate()):
        logging.warning("WebSocket 线程已运行，跳过启动")
        update_queue.put(("status_label", "WebSocket 线程已运行"))
        return

    websocket_thread_running = True
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(init_websocket(api_key, api_secret, passphrase))
    except Exception as e:
        logging.error(f"WebSocket 启动失败: {str(e)}\n{traceback.format_exc()}")
        update_queue.put(("status_label", f"WebSocket 启动失败: {str(e)}"))
    finally:
        websocket_thread_running = False
        if loop and not loop.is_closed():
            loop.close()
            logging.info("asyncio 事件循环已关闭")

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
        ticker = market_client.get_ticker(instId='BTC-USDT-SWAP')
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


def get_klines(symbol, okx_interval, limit=100):
    global market_client
    try:
        if market_client is None:
            config = load_config()
            api_key = config.get('api_key')
            api_secret = config.get('api_secret')
            passphrase = config.get('passphrase')
            if not all([api_key, api_secret, passphrase]):
                logging.error("API 密钥未配置")
                return None
            if not init_okx(api_key, api_secret, passphrase, flag='0'):
                logging.error("无法初始化 OKX 客户端")
                return None
        server_time = get_server_time()
        if not server_time:
            logging.warning("无法获取服务器时间")
            return None
        klines = market_client.get_candlesticks(instId=symbol, bar=okx_interval, limit=str(limit))
        logging.debug(f"获取K线数据原始响应: {klines}")
        if not klines.get('data'):
            logging.warning(f"无K线数据（{symbol}, {okx_interval}）, API 响应: {klines}")
            return None
        df = pd.DataFrame(klines['data'],
                          columns=['ts', 'open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote', 'confirm'])
        df['ts'] = pd.to_datetime(df['ts'].astype(float), unit='ms')
        df[['open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote']] = df[
            ['open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote']].astype(float)
        df = df.sort_values('ts').reset_index(drop=True)
        if df.empty:
            logging.warning(f"无有效K线数据（{symbol}, {okx_interval}）")
            return None

        latest_kline_time = df['ts'].iloc[-1]
        time_diff = (server_time.timestamp() - df['ts'].iloc[-1].timestamp())
        timeframe_to_max_age = {
            '1m': 3600, '5m': 7200, '15m': 14400, '30m': 28800, '1H': 86400, '4H': 172800, '1D': 604800
        }
        max_age = timeframe_to_max_age.get(okx_interval, 172800)
        if time_diff > max_age:
            logging.warning(
                f"K线数据过旧，最新时间: {latest_kline_time}, 服务器时间: {server_time}, 时间差: {time_diff}秒")
            return None

        logging.info(
            f"获取K线数据: {len(df)} 条, 最新K线时间: {latest_kline_time}, 收盘价: {df['close'].iloc[-1]:.2f}, 未闭合: {df['confirm'].iloc[-1] != '1'}")
        logging.debug(f"K线数据最后5条: {df.tail(5)}")
        return df
    except Exception as e:
        logging.error(f"获取K线错误: {str(e)}\n{traceback.format_exc()}")
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
        if pd.isna(rsi.iloc[-1]):
            logging.warning("RSI 计算结果无效")
            return None
        logging.info(f"RSI 计算: 周期={period}, 最新RSI={rsi.iloc[-1]:.2f}")
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

def get_symbol_info(symbol='BTC-USDT-SWAP'):
    rate_limit()
    try:
        info = public_client.get_instruments(instType='SWAP', instId=symbol)
        if info.get('code') != '0':
            raise Exception(f"获取交易对信息失败: {info.get('msg', '未知错误')}")
        ct_val = float(info['data'][0]['ctVal'])
        min_qty = float(info['data'][0]['minSz'])
        return ct_val, min_qty
    except Exception as e:
        update_queue.put(("status_label", f"获取交易对信息失败: {str(e)}"))
        logging.error(f"获取交易对信息失败: {str(e)}")
        return 0.0001, 1

def place_order(side, pos_side, quantity, leverage, margin_mode, take_profit=None, stop_loss=None, tp_sl_mode='exchange'):
    try:
        ct_val, min_qty = get_symbol_info('BTC-USDT-SWAP')
        quantity = max(round(quantity / ct_val), 1)
        if quantity < min_qty:
            update_queue.put(("status_label", f"下单失败: 数量 {quantity} 张小于最小值 {min_qty} 张"))
            logging.warning(f"下单失败: 数量 {quantity} 张小于最小值 {min_qty} 张")
            return None
        set_leverage('BTC-USDT-SWAP', leverage, margin_mode)
        params = {
            'instId': 'BTC-USDT-SWAP',
            'tdMode': margin_mode,
            'side': side.lower(),
            'posSide': pos_side.lower(),
            'ordType': 'market',
            'sz': str(quantity),
            'clOrdId': f"order_{int(time.time())}"
        }
        if tp_sl_mode == 'exchange' and take_profit is not None and stop_loss is not None:
            if pos_side == 'long':
                params['tpTriggerPx'] = str(round(current_price * (1 + take_profit / 100), 2))
                params['tpOrdPx'] = '-1'  # 市价止盈
                params['slTriggerPx'] = str(round(current_price * (1 - stop_loss / 100), 2))
                params['slOrdPx'] = '-1'  # 市价止损
            elif pos_side == 'short':
                params['tpTriggerPx'] = str(round(current_price * (1 - take_profit / 100), 2))
                params['tpOrdPx'] = '-1'
                params['slTriggerPx'] = str(round(current_price * (1 + stop_loss / 100), 2))
                params['slOrdPx'] = '-1'
        order = trade_client.place_order(**params)
        if order['code'] == '0':
            action = '开多' if side == 'buy' and pos_side == 'long' else '开空' if side == 'sell' and pos_side == 'short' else '平仓'
            tp_sl_text = f", 止盈止损: {tp_sl_mode}" if tp_sl_mode == 'exchange' and take_profit is not None else ""
            update_queue.put(("status_label", f"{action} 订单已下: 数量 {quantity} 张{tp_sl_text}"))
            logging.info(f"{action} 订单已下: {quantity} 张{tp_sl_text}")
            return order['data'][0]['ordId']
        else:
            update_queue.put(("status_label", f"下单失败: {order['msg']}"))
            logging.error(f"下单失败: {order['msg']}")
            return None
    except Exception as e:
        update_queue.put(("status_label", f"下单失败: {str(e)}"))
        logging.error(f"下单失败: {str(e)}\n{traceback.format_exc()}")
        return None

def get_balance():
    rate_limit()
    try:
        balance = account_client.get_account_balance()
        if balance.get('code') != '0':
            raise Exception(f"获取余额失败: {balance.get('msg', '未知错误')}")
        usdt = float(
            next((asset for asset in balance['data'][0]['details'] if asset['ccy'] == 'USDT'), {'availEq': '0'})['availEq'])
        total_equity = float(balance['data'][0]['totalEq'])
        positions = account_client.get_positions(instType='SWAP', instId='BTC-USDT-SWAP')
        long_qty = 0
        short_qty = 0
        long_avg_price = 0.0
        short_avg_price = 0.0
        if positions.get('code') == '0' and positions.get('data'):
            for pos in positions['data']:
                if pos['instId'] == 'BTC-USDT-SWAP':
                    qty = float(pos['pos'])
                    avg_price = float(pos['avgPx']) if pos['avgPx'] else 0.0
                    if pos['posSide'] == 'long':
                        long_qty = qty
                        long_avg_price = avg_price
                    elif pos['posSide'] == 'short':
                        short_qty = qty
                        short_avg_price = avg_price
        return usdt, long_qty, short_qty, long_avg_price, short_avg_price, total_equity
    except Exception as e:
        update_queue.put(("status_label", f"获取余额失败: {str(e)}"))
        logging.error(f"获取余额失败: {str(e)}")
        return None, None, None, None, None, None

def check_take_profit_stop_loss(long_qty, short_qty, long_avg_price, short_avg_price, current_price, take_profit, stop_loss):
    try:
        pos_side = None
        qty = 0
        reason = None
        if long_qty > 0 and long_avg_price > 0:
            profit_percentage = (current_price - long_avg_price) / long_avg_price * 100
            if profit_percentage >= take_profit:
                pos_side = 'long'
                qty = long_qty
                reason = '止盈'
            elif profit_percentage <= -stop_loss:
                pos_side = 'long'
                qty = long_qty
                reason = '止损'
        if short_qty > 0 and short_avg_price > 0:
            profit_percentage = (short_avg_price - current_price) / short_avg_price * 100
            if profit_percentage >= take_profit:
                pos_side = 'short'
                qty = short_qty
                reason = '止盈'
            elif profit_percentage <= -stop_loss:
                pos_side = 'short'
                qty = short_qty
                reason = '止损'
        return pos_side, qty, reason
    except Exception as e:
        logging.error(f"检查止盈止损错误: {str(e)}\n{traceback.format_exc()}")
        return None, 0, None

def rsi_trading():
    global last_rsi_buy_time, last_rsi_sell_time, latest_rsi, current_price, price_update_time
    if not rsi_enabled:
        logging.info("RSI 交易未启用，跳过")
        return
    try:
        logging.info(f"开始 RSI 交易检查（周期={rsi_timeframe}）")
        df = get_klines('BTC-USDT-SWAP', rsi_timeframe)
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
        usdt_balance, long_qty, short_qty, long_avg_price, short_avg_price, total_equity = get_balance()
        if usdt_balance is None:
            logging.warning("RSI 交易跳过: 获取余额失败")
            return
        with lock:
            if current_price <= 0:
                price = get_btc_price()
                if price:
                    current_price = price
                    price_update_time = datetime.now()
                    logging.info(f"RSI 交易获取价格: ${price:.2f}")
                else:
                    logging.warning("RSI 交易跳过: 无法获取价格")
                    return
            timeframe_to_cooldown = {
                '1m': timedelta(minutes=2), '5m': timedelta(minutes=5), '15m': timedelta(minutes=15),
                '30m': timedelta(minutes=30), '1h': timedelta(hours=1), '4h': timedelta(hours=2), '1d': timedelta(days=1)
            }
            cooldown = timeframe_to_cooldown.get(rsi_timeframe, timedelta(hours=1)) * rsi_cooldown_multiplier
            now = datetime.now()
            # 仅在程序计算模式下检查止盈止损
            if rsi_tp_sl_mode == 'program':
                pos_side, qty, reason = check_take_profit_stop_loss(
                    long_qty, short_qty, long_avg_price, short_avg_price, current_price, rsi_take_profit, rsi_stop_loss
                )
                if pos_side and qty > 0:
                    order = place_order('sell' if pos_side == 'long' else 'buy', pos_side, qty, rsi_leverage, rsi_margin_mode, tp_sl_mode='program')
                    if order:
                        update_queue.put(("status_label", f"RSI {reason}, 平仓 {qty:.0f} 张"))
                        logging.info(f"RSI {reason}, 平仓 {qty:.0f} 张")
                        return
            max_quantity = (total_equity * rsi_max_position_ratio / 100) / current_price * rsi_leverage
            if (rsi_trade_direction in ['both', 'long'] and
                latest_rsi <= rsi_buy_value and
                (last_rsi_buy_time is None or (now - last_rsi_buy_time) >= cooldown)):
                quantity = min((usdt_balance * rsi_buy_ratio / 100) / current_price * rsi_leverage, max_quantity)
                if quantity <= 0:
                    update_queue.put(("status_label", "RSI 开多失败: 余额不足"))
                    logging.warning("RSI 开多失败: 余额不足")
                    return
                order = place_order('buy', 'long', quantity, rsi_leverage, rsi_margin_mode, rsi_take_profit, rsi_stop_loss, rsi_tp_sl_mode)
                if order:
                    with lock:
                        last_rsi_buy_time = now
                    update_queue.put(("status_label", f"RSI 开多成功: 数量 {quantity:.0f} 张"))
            if (rsi_trade_direction in ['both', 'short'] and
                latest_rsi >= rsi_sell_value and
                (last_rsi_sell_time is None or (now - last_rsi_sell_time) >= cooldown)):
                if long_qty > 0 and rsi_trade_direction in ['both', 'long']:
                    order = place_order('sell', 'long', long_qty, rsi_leverage, rsi_margin_mode, tp_sl_mode=rsi_tp_sl_mode)
                    if order:
                        with lock:
                            last_rsi_sell_time = now
                        update_queue.put(("status_label", f"RSI 平多成功: 数量 {long_qty:.0f} 张"))
                elif short_qty == 0:
                    quantity = min((usdt_balance * rsi_buy_ratio / 100) / current_price * rsi_leverage, max_quantity)
                    order = place_order('sell', 'short', quantity, rsi_leverage, rsi_margin_mode, rsi_take_profit, rsi_stop_loss, rsi_tp_sl_mode)
                    if order:
                        with lock:
                            last_rsi_sell_time = now
                        update_queue.put(("status_label", f"RSI 开空成功: 数量 {quantity:.0f} 张"))
    except Exception as e:
        logging.error(f"RSI 交易错误: {str(e)}\n{traceback.format_exc()}")
        update_queue.put(("status_label", f"RSI 交易错误: {str(e)}"))

def macd_trading():
    global last_macd_buy_time, last_macd_sell_time, latest_macd, latest_signal, latest_histogram, current_price, price_update_time
    if not macd_enabled:
        logging.info("MACD 交易未启用，跳过")
        return
    try:
        logging.info(f"开始 MACD 交易检查（周期={macd_timeframe}）")
        df = get_klines('BTC-USDT-SWAP', macd_timeframe)
        if df is None:
            logging.warning("MACD 交易跳过: 无K线数据")
            return
        macd, signal, histogram = calculate_macd(df)
        if macd is None or signal is None or histogram is None:
            logging.warning("MACD 交易跳过: MACD 计算失败")
            return
        latest_macd_value = macd.iloc[-1]
        latest_signal_value = signal.iloc[-1]
        latest_histogram_value = histogram.iloc[-1]
        if pd.isna(latest_macd_value) or pd.isna(latest_signal_value) or pd.isna(latest_histogram_value):
            logging.warning("MACD 交易跳过: 无效 MACD 值")
            return
        with lock:
            latest_macd = latest_macd_value
            latest_signal = latest_signal_value
            latest_histogram = latest_histogram_value
        usdt_balance, long_qty, short_qty, long_avg_price, short_avg_price, total_equity = get_balance()
        if usdt_balance is None:
            logging.warning("MACD 交易跳过: 获取余额失败")
            return
        with lock:
            if current_price <= 0:
                price = get_btc_price()
                if price:
                    current_price = price
                    price_update_time = datetime.now()
                    logging.info(f"MACD 交易获取价格: ${price:.2f}")
                else:
                    logging.warning("MACD 交易跳过: 无法获取价格")
                    return
            timeframe_to_cooldown = {
                '1m': timedelta(minutes=2), '5m': timedelta(minutes=5), '15m': timedelta(minutes=15),
                '30m': timedelta(minutes=30), '1h': timedelta(hours=1), '4h': timedelta(hours=2), '1d': timedelta(days=1)
            }
            cooldown = timeframe_to_cooldown.get(macd_timeframe, timedelta(hours=1)) * macd_cooldown_multiplier
            now = datetime.now()
            # 仅在程序计算模式下检查止盈止损
            if macd_tp_sl_mode == 'program':
                pos_side, qty, reason = check_take_profit_stop_loss(
                    long_qty, short_qty, long_avg_price, short_avg_price, current_price, macd_take_profit, macd_stop_loss
                )
                if pos_side and qty > 0:
                    order = place_order('sell' if pos_side == 'long' else 'buy', pos_side, qty, macd_leverage, macd_margin_mode, tp_sl_mode='program')
                    if order:
                        update_queue.put(("status_label", f"MACD {reason}, 平仓 {qty:.0f} 张"))
                        logging.info(f"MACD {reason}, 平仓 {qty:.0f} 张")
                        return
            max_quantity = (total_equity * macd_max_position_ratio / 100) / current_price * macd_leverage
            # 金叉开多
            if (macd_trade_direction in ['both', 'long'] and
                latest_macd > latest_signal and
                macd.iloc[-2] <= signal.iloc[-2] and
                (last_macd_buy_time is None or (now - last_macd_buy_time) >= cooldown)):
                quantity = min((usdt_balance * macd_buy_ratio / 100) / current_price * macd_leverage, max_quantity)
                if quantity <= 0:
                    update_queue.put(("status_label", "MACD 开多失败: 余额不足"))
                    logging.warning("MACD 开多失败: 余额不足")
                    return
                order = place_order('buy', 'long', quantity, macd_leverage, macd_margin_mode, macd_take_profit, macd_stop_loss, macd_tp_sl_mode)
                if order:
                    with lock:
                        last_macd_buy_time = now
                    update_queue.put(("status_label", f"MACD 开多成功: 数量 {quantity:.0f} 张"))
            # 死叉开空
            if (macd_trade_direction in ['both', 'short'] and
                latest_macd < latest_signal and
                macd.iloc[-2] >= signal.iloc[-2] and
                (last_macd_sell_time is None or (now - last_macd_sell_time) >= cooldown)):
                if long_qty > 0 and macd_trade_direction in ['both', 'long']:
                    order = place_order('sell', 'long', long_qty, macd_leverage, macd_margin_mode, tp_sl_mode=macd_tp_sl_mode)
                    if order:
                        with lock:
                            last_macd_sell_time = now
                        update_queue.put(("status_label", f"MACD 平多成功: 数量 {long_qty:.0f} 张"))
                elif short_qty == 0:
                    quantity = min((usdt_balance * macd_buy_ratio / 100) / current_price * macd_leverage, max_quantity)
                    order = place_order('sell', 'short', quantity, macd_leverage, macd_margin_mode, macd_take_profit, macd_stop_loss, macd_tp_sl_mode)
                    if order:
                        with lock:
                            last_macd_sell_time = now
                        update_queue.put(("status_label", f"MACD 开空成功: 数量 {quantity:.0f} 张"))
    except Exception as e:
        logging.error(f"MACD 交易错误: {str(e)}\n{traceback.format_exc()}")
        update_queue.put(("status_label", f"MACD 交易错误: {str(e)}"))

def trading_loop():
    global running, animation_frame, current_price, price_update_time
    logging.info("交易循环开始")
    base_interval_seconds = 5
    if rsi_timeframe in ['1m', '5m'] or macd_timeframe in ['1m', '5m']:
        base_interval_seconds = 3
    timeframe_to_interval = {
        '1m': 5, '5m': 10, '15m': 10, '30m': 10, '1h': 30, '4h': 60, '1d': 300
    }
    rsi_interval = timeframe_to_interval.get(rsi_timeframe, 60) if rsi_enabled else float('inf')
    macd_interval = timeframe_to_interval.get(macd_timeframe, 60) if macd_enabled else float('inf')
    kline_interval_seconds = min(rsi_interval, macd_interval)
    logging.info(f"K线请求间隔: {kline_interval_seconds}秒 (RSI={rsi_timeframe}, MACD={macd_timeframe})")
    last_kline_request = time.time()
    last_price_update = time.time()
    while running:
        try:
            current_time = time.time()
            # 备用价格更新
            if current_time - last_price_update > 30:  # 每30秒检查一次
                price = get_btc_price()
                if price:
                    with lock:
                        current_price = price
                        price_update_time = datetime.now()
                    update_queue.put(("price_label", f"当前 BTC 价格: ${price:.2f}", (255, 255, 255)))
                    logging.info(f"HTTP 价格更新: ${price:.2f}")
                last_price_update = current_time
            animation_frame += 1
            color = (255, 255, 0) if animation_frame % 5 == 0 else (0, 255, 255)
            update_queue.put(("price_label", f"当前 BTC 价格: ${current_price:.2f}", color))
            usdt_balance, long_qty, short_qty, _, _, total_equity = get_balance()
            if usdt_balance is not None:
                update_queue.put(("balance_label", f"余额: {usdt_balance:.2f} USDT, 多仓: {long_qty:.0f} 张, 空仓: {short_qty:.0f} 张", (255, 255, 255)))
            else:
                update_queue.put(("balance_label", "余额: 获取失败", (255, 0, 0)))
            if current_time - last_kline_request >= kline_interval_seconds:
                if rsi_enabled:
                    rsi_trading()
                if macd_enabled:
                    macd_trading()
                last_kline_request = current_time
            gc.collect()
            time.sleep(base_interval_seconds)
        except Exception as e:
            logging.error(f"交易循环错误: {str(e)}\n{traceback.format_exc()}")
            update_queue.put(("status_label", f"交易错误: {str(e)}"))
    logging.info("交易循环退出")

def update_ui_callback():
    global last_ui_log_time
    try:
        if not dpg.is_dearpygui_running():
            return
        current_time = time.time()
        start_time = current_time
        while not update_queue.empty() and current_time - start_time < 0.1:  # 超时 100ms
            item = update_queue.get_nowait()
            tag, value, color = item if len(item) == 3 else (item[0], item[1], None)
            if value is not None and dpg.does_item_exist(tag):
                dpg.set_value(tag, value)
            if color is not None and dpg.does_item_exist(tag):
                with dpg.theme() as temp_theme:
                    with dpg.theme_component(dpg.mvAll):
                        dpg.add_theme_color(dpg.mvThemeCol_Text, color)
                    dpg.bind_item_theme(tag, temp_theme)
            current_time = time.time()
        if running and current_time - last_ui_log_time >= 2:
            with lock:
                if latest_rsi is not None and not pd.isna(latest_rsi):
                    rsi_text = f"RSI(14): {latest_rsi:.2f}"
                    rsi_color = (255, 0, 0) if latest_rsi >= rsi_sell_value else (0, 255, 0) if latest_rsi <= rsi_buy_value else (255, 255, 255)
                    update_queue.put(("rsi_display", rsi_text, rsi_color))
                    logging.info(f"Updating RSI display: {rsi_text}")
                else:
                    update_queue.put(("rsi_display", "RSI(14): Waiting for data", (255, 165, 0)))
                if latest_macd is not None and not pd.isna(latest_macd):
                    macd_text = f"MACD: {latest_macd:.2f}, Signal: {latest_signal:.2f}, Histogram: {latest_histogram:.2f}"
                    update_queue.put(("macd_display", f"MACD(12,26,9): {latest_macd:.2f}", (0, 255, 0) if latest_macd > 0 else (255, 0, 0)))
                    update_queue.put(("signal_display", f"Signal(9): {latest_signal:.2f}", (255, 255, 255)))
                    update_queue.put(("histogram_display", f"Histogram: {latest_histogram:.2f}", (0, 255, 0) if latest_histogram > 0 else (255, 0, 0)))
                    logging.info(f"Updating MACD: {macd_text}")
                else:
                    update_queue.put(("macd_display", "MACD(12,26,9): Waiting for data", (255, 165, 0)))
            last_ui_log_time = current_time
            logging.info("UI update processed")
    except Exception as e:
        logging.error(f"UI update error: {str(e)}\n{traceback.format_exc()}")
        update_queue.put(("status_label", f"UI update failed: {str(e)}"))

def start_trading():
    global running
    try:
        if not trade_client:
            update_queue.put(("status_label", "请输入有效的 API 密钥并保存"))
            logging.error("API 密钥未初始化")
            return
        if not ws_connected:
            update_queue.put(("status_label", "WebSocket 未连接，请先保存 API 密钥"))
            logging.error("WebSocket 未连接")
            return
        if not rsi_enabled and not macd_enabled:
            update_queue.put(("status_label", "请启用 RSI 或 MACD 交易"))
            logging.warning("未启用任何交易策略")
            return
        set_leverage('BTC-USDT-SWAP', rsi_leverage if rsi_enabled else macd_leverage,
                     rsi_margin_mode if rsi_enabled else macd_margin_mode)
        price = get_btc_price()
        if price:
            with lock:
                global current_price, price_update_time
                current_price = price
                price_update_time = datetime.now()
            update_queue.put(("price_label", f"当前 BTC 价格: ${price:.2f}", (255, 255, 255)))
            logging.info(f"初始价格更新: ${price:.2f}")
        running = True
        if rsi_enabled:
            rsi_trading()
        if macd_enabled:
            macd_trading()
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
        logging.info("交易停止")
    except Exception as e:
        logging.error(f"停止交易错误: {str(e)}\n{traceback.format_exc()}")
        update_queue.put(("status_label", f"交易停止失败: {str(e)}"))

def save_settings():
    global rsi_enabled, macd_enabled, rsi_buy_ratio, macd_buy_ratio, rsi_timeframe, macd_timeframe, rsi_buy_value, rsi_sell_value
    global rsi_leverage, macd_leverage, rsi_margin_mode, macd_margin_mode
    global rsi_take_profit, rsi_stop_loss, macd_take_profit, macd_stop_loss
    global rsi_max_position_ratio, macd_max_position_ratio, rsi_trade_direction, macd_trade_direction
    global rsi_cooldown_multiplier, macd_cooldown_multiplier
    try:
        # 术语映射
        margin_mode_map = {'全仓': 'cross', '逐仓': 'isolated'}
        trade_direction_map = {'双向': 'both', '仅多头': 'long', '仅空头': 'short'}

        rsi_enabled = dpg.get_value("rsi_enabled")
        macd_enabled = dpg.get_value("macd_enabled")
        rsi_buy_ratio = dpg.get_value("rsi_buy_ratio")
        macd_buy_ratio = dpg.get_value("macd_buy_ratio")
        rsi_timeframe = timeframe_display_map.get(dpg.get_value("rsi_timeframe"), '1h')
        macd_timeframe = timeframe_display_map.get(dpg.get_value("macd_timeframe"), '1h')
        rsi_buy_value = dpg.get_value("rsi_buy_value")
        rsi_sell_value = dpg.get_value("rsi_sell_value")
        rsi_leverage = dpg.get_value("rsi_leverage")
        macd_leverage = dpg.get_value("macd_leverage")
        rsi_margin_mode = margin_mode_map.get(dpg.get_value("rsi_margin_mode"), 'cross')
        macd_margin_mode = margin_mode_map.get(dpg.get_value("macd_margin_mode"), 'cross')
        rsi_take_profit = dpg.get_value("rsi_take_profit")
        rsi_stop_loss = dpg.get_value("rsi_stop_loss")
        macd_take_profit = dpg.get_value("macd_take_profit")
        macd_stop_loss = dpg.get_value("macd_stop_loss")
        rsi_max_position_ratio = dpg.get_value("rsi_max_position_ratio")
        macd_max_position_ratio = dpg.get_value("macd_max_position_ratio")
        rsi_trade_direction = trade_direction_map.get(dpg.get_value("rsi_trade_direction"), 'both')
        macd_trade_direction = trade_direction_map.get(dpg.get_value("macd_trade_direction"), 'both')
        rsi_cooldown_multiplier = dpg.get_value("rsi_cooldown_multiplier")
        macd_cooldown_multiplier = dpg.get_value("macd_cooldown_multiplier")
        update_queue.put(("status_label", "设置已保存"))
        logging.info("设置已保存")
    except Exception as e:
        logging.error(f"保存设置错误: {str(e)}\n{traceback.format_exc()}")
        update_queue.put(("status_label", f"设置保存失败: {str(e)}"))

def save_api():
    global trade_client, market_client, account_client, public_client, ws, ws_connected
    try:
        api_key = dpg.get_value("api_key")
        api_secret = dpg.get_value("api_secret")
        passphrase = dpg.get_value("passphrase")
        logging.info(f"尝试保存 API 密钥: api_key={api_key[:4]}****, api_secret={api_secret[:4]}****, passphrase={passphrase[:4]}****")
        if not all([api_key, api_secret, passphrase]):
            update_queue.put(("status_label", "API 密钥不能为空"))
            logging.error("API 密钥不能为空")
            return False
        # 检查是否已初始化
        if trade_client and ws_connected:
            config = load_config()
            if (config.get('api_key') == api_key and config.get('api_secret') == api_secret and
                config.get('passphrase') == passphrase):
                update_queue.put(("status_label", "API 已初始化，无需重复保存"))
                logging.info("API 已初始化，跳过重复保存")
                return True
        success = False
        for flag in ['0', '1']:
            if init_okx(api_key, api_secret, passphrase, flag):
                save_config(api_key, api_secret, passphrase)
                if not ws_connected:
                    threading.Thread(target=start_websocket, args=(api_key, api_secret, passphrase), daemon=True).start()
                    timeout = 10
                    start_time = time.time()
                    while time.time() - start_time < timeout and not ws_connected:
                        time.sleep(1)
                if ws_connected:
                    update_queue.put(("status_label", f"API 已保存，WebSocket 已连接 (flag={flag})"))
                    logging.info(f"API 已保存，WebSocket 已连接 (flag={flag})")
                    usdt, long_qty, short_qty, _, _, _ = get_balance()
                    if usdt is not None:
                        update_queue.put(("balance_label", f"余额: {usdt:.2f} USDT, 多仓: {long_qty:.0f} 张, 空仓: {short_qty:.0f} 张", (255, 255, 255)))
                    else:
                        update_queue.put(("balance_label", "余额: 获取失败", (255, 0, 0)))
                    success = True
                    break
                else:
                    update_queue.put(("status_label", f"API 保存成功，但 WebSocket 连接失败 (flag={flag})"))
                    logging.warning(f"WebSocket 连接失败 (flag={flag})")
            else:
                update_queue.put(("status_label", f"无效的 API 密钥 (flag={flag})"))
                logging.error(f"无效的 API 密钥 (flag={flag})")
        if not success:
            update_queue.put(("status_label", "API 保存失败：无效的密钥"))
        return success
    except Exception as e:
        logging.error(f"保存 API 错误: {str(e)}\n{traceback.format_exc()}")
        update_queue.put(("status_label", f"API 保存失败: {str(e)}"))
        return False

def button_animation(sender):
    original_color = (255, 69, 0, 255)  # 原始按钮颜色
    highlight_color = (255, 165, 0, 255)  # 高亮颜色
    # 应用高亮颜色
    with dpg.theme() as temp_theme:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, highlight_color)
        dpg.bind_item_theme(sender, temp_theme)
    time.sleep(0.1)  # 短暂延迟以显示高亮效果
    # 恢复原始颜色
    with dpg.theme() as restore_theme:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, original_color)
        dpg.bind_item_theme(sender, restore_theme)
def show_help():
    if dpg.does_item_exist("help_window"):
        dpg.delete_item("help_window")
    with dpg.window(label="使用说明书", tag="help_window", width=650, height=600, pos=(100, 100), no_scrollbar=False):
        dpg.add_text("BTC 金丝木量化交易机器人 - OKX 合约", color=(0, 255, 255))
        dpg.add_separator()
        dpg.add_text("1 交易逻辑", color=(255, 215, 0))
        dpg.add_text("本程序基于 RSI 和 MACD 指标进行自动化合约交易 适用于 OKX 平台的 BTC-USDT-SWAP")
        dpg.add_text("- RSI 交易逻辑")
        dpg.add_text("  * 开多 当 RSI(14) 小于等于 30 且距离上次开多超过冷却时间")
        dpg.add_text("  * 开空或平多 当 RSI(14) 大于等于 70 且距离上次操作超过冷却时间")
        dpg.add_text("  * 止盈或止损 当持仓盈亏达到设定百分比时自动平仓")
        dpg.add_text("- MACD 交易逻辑")
        dpg.add_text("  * 开多 当 MACD 线 < 0 MACD 线从下方穿过信号线-金叉 且柱状图 > 0")
        dpg.add_text("  * 开空或平多 当 MACD 线 > 0 MACD 线从上方穿过信号线-死叉 且柱状图 < 0")
        dpg.add_text("2 交易设置项说明", color=(255, 215, 0))
        with dpg.group():
            dpg.add_text("- 杠杆倍数 设置合约杠杆 1x-10x RSI 和 MACD 可分别设置")
            dpg.add_text("- 保证金模式 选择全仓 cross 或 逐仓 isolated RSI 和 MACD 可分别设置")
            dpg.add_text("- 止盈比例 当持仓盈利达到指定百分比时平仓 例如 10%")
            dpg.add_text("- 止损比例 当持仓亏损达到指定百分比时平仓 例如 5%")
            dpg.add_text("- 最大持仓比例 限制单次开仓占总保证金的比例 例如 50%")
            dpg.add_text("- 交易方向 选择双向 仅多头或仅空头")
            dpg.add_text("- 冷却时间倍数 调整冷却时间长度 例如 1.5 倍")
        dpg.add_separator()
        dpg.add_text("3 使用方式", color=(255, 255, 0))
        dpg.add_text("操作步骤")
        dpg.add_text("-1 在 OKX API 设置中输入 API Key, Secret Key 和 Passphrase 然后点击保存密钥")
        dpg.add_text("- 确保 API 密钥具有交易 余额查询和持仓查询权限")
        dpg.add_text("-2 在 RSI 交易设置 和 MACD 交易设置中配置参数")
        dpg.add_text("- 勾选 启用 RSI 交易 或 启用 MACD 交易 以激活相应策略")
        dpg.add_text("- 设置单次买入比例 决定每次开仓使用的资金比例")
        dpg.add_text("- 选择时间周期 确定指标计算的 K线周期 例如 1小时")
        dpg.add_text("- 设置杠杆倍数 保证金模式 止盈比例 止损比例 最大持仓比例 交易方向 和冷却时间倍数")
        dpg.add_text("-3 点击保存设置 确认参数 必须操作")
        dpg.add_text("-4 点击启动交易 开始自动化交易")
        dpg.add_text("-5 点击停止交易 暂停交易")
        dpg.add_text("-6 查看 指标实时监控 了解当前的 RSI 和 MACD 值")
        dpg.add_text("注意事项")
        dpg.add_text("- 确保账户有足够的 USDT 可用余额以执行交易")
        dpg.add_text("- 交易日志保存在 websocket.log 可用于调试")
        dpg.add_text("- WebSocket 提供实时价格和持仓更新 若断开会自动重连")
        dpg.add_text("- 杠杆交易有高风险 请谨慎设置杠杆倍数和止损")
        dpg.add_separator()
        dpg.add_text("4 动态冷却时间", color=(255, 215, 0))
        dpg.add_text("为避免过于频繁交易 程序为 RSI 和 MACD 设置动态冷却时间")
        dpg.add_text("- 1分钟 冷却时间 1分钟")
        dpg.add_text("- 5分钟 冷却时间 5分钟")
        dpg.add_text("- 15分钟 冷却时间 15分钟")
        dpg.add_text("- 30分钟 冷却时间 30分钟")
        dpg.add_text("- 1小时 冷却时间 1小时")
        dpg.add_text("- 4小时 冷却时间 4小时")
        dpg.add_text("- 1日 冷却时间 1日")
        dpg.add_separator()
        dpg.add_text("5 数据&刷新冷却时间", color=(255, 215, 0))
        dpg.add_text("为避免过于频繁请求 程序为数据请求设置了动态冷却时间")
        dpg.add_text("- 1分钟 冷却时间 5秒 ")
        dpg.add_text("- 5分钟 冷却时间 10秒")
        dpg.add_text("- 15分钟 冷却时间 10秒")
        dpg.add_text("- 30分钟 冷却时间 10秒")
        dpg.add_text("- 1小时 冷却时间 30秒")
        dpg.add_text("- 4小时 冷却时间 1分钟")
        dpg.add_text("- 1日 冷却时间 5分钟")
        dpg.add_text("强烈不推荐你玩1分钟的,程序性能优化不好数据有几秒的延迟,会影响开单", color=(128, 128, 128))
        dpg.add_separator()

        dpg.add_text("想支持树酱的话可以向以下钱包地址捐款-", color=(128, 128, 128))
        dpg.add_text("0x4FdFCfc03A5416EB5D9B85F4bad282c0DaF19783", color=(128, 128, 128))
        dpg.add_text("感谢你的支持呀 -不捐也没关系 作者会自己找垃圾吃的~", color=(128, 128, 128))


def create_gui():
    global last_ui_log_time
    logging.info("Configuring DearPyGui context")
    try:
        dpg.create_context()
        icon_path = os.path.join(os.path.dirname(__file__), "jio.ico")
        if not os.path.exists(icon_path):
            logging.warning(f"Icon file not found: {icon_path}")
            icon_path = ""
        dpg.create_viewport(
            title='Tree Bot',
            width=800,
            height=1020,
            small_icon=icon_path,
            large_icon=icon_path
        )
        font_path = 'C:\\Windows\\Fonts\\SimHei.ttf'  # 优先使用 SimHei.ttf
        if not os.path.exists(font_path):
            logging.warning(f"Font file missing: {font_path}")
            font_path = os.path.join(os.path.dirname(__file__), 'NotoSerifCJKsc-Bold.otf')
            if not os.path.exists(font_path):
                logging.error(f"Backup font missing: {font_path}")
                font_path = None
                logging.info("No font loaded, using default system font")

        with dpg.font_registry():
            if font_path:
                try:
                    with dpg.font(font_path, 24) as title_font:
                        dpg.add_font_range(0x4E00, 0x9FFF)
                    with dpg.font(font_path, 16) as body_font:
                        dpg.add_font_range(0x4E00, 0x9FFF)
                    dpg.bind_font(body_font)
                except Exception as e:
                    logging.error(f"Font loading failed: {str(e)}")
                    title_font = None
                    body_font = None
            else:
                title_font = None
                body_font = None

        with dpg.theme() as global_theme:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, (30, 30, 50, 255))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 255, 255, 255))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (50, 50, 70, 255))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (70, 70, 90, 255))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, (90, 90, 110, 255))
                dpg.add_theme_color(dpg.mvThemeCol_CheckMark, (0, 255, 0, 255))
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrab, (255, 215, 0, 255))
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrabActive, (255, 255, 0, 255))
        with dpg.theme() as button_theme:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (255, 69, 0, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (255, 99, 71, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (255, 140, 0, 255))
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 10)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 10, 5)
        with dpg.theme() as section_theme:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_Text, (0, 255, 255, 255))
        with dpg.theme() as table_theme:
            with dpg.theme_component(dpg.mvTable):
                dpg.add_theme_style(dpg.mvStyleVar_CellPadding, 5, 5)
                dpg.add_theme_color(dpg.mvThemeCol_TableBorderStrong, (100, 100, 100, 255))
                dpg.add_theme_color(dpg.mvThemeCol_TableBorderLight, (80, 80, 80, 255))
        dpg.bind_theme(global_theme)

        with dpg.window(label="树酱提示 本软件完全免费开源 你从任何途径购买都说明被骗了", width=820, height=1000, pos=(0, 0), no_scrollbar=False):
            # 标题区域
            with dpg.group():
                title_text = "BTC 金丝木量化交易 - OKX 合约"
                title_width = len(title_text) * 38
                with dpg.drawlist(width=title_width, height=28):
                    for i in range(title_width):
                        t = i / title_width
                        r = int(255 * (1 - t))
                        g = int(215 + 40 * t)
                        b = int(0 + 255 * t)
                        dpg.draw_line((i, 0), (i, 40), color=(r, g, b, 255))
                    dpg.draw_text((0, 0), title_text, size=24)
                if title_font:
                    dpg.bind_item_font(dpg.last_item(), title_font)

            # API 设置
            with dpg.group():
                dpg.add_text("OKX API 设置")
                dpg.bind_item_theme(dpg.last_item(), section_theme)
                with dpg.table(header_row=False, borders_outerV=True, borders_innerV=True, borders_outerH=True, borders_innerH=True):
                    dpg.add_table_column()
                    dpg.add_table_column()
                    with dpg.table_row():
                        dpg.add_text("API key-密钥")
                        dpg.add_input_text(tag="api_key", default_value=load_config().get('api_key', ''), width=300)
                    with dpg.table_row():
                        dpg.add_text("API secret-私钥")
                        dpg.add_input_text(tag="api_secret", default_value=load_config().get('api_secret', ''), password=True, width=300)
                    with dpg.table_row():
                        dpg.add_text("passphrase-密码短语")
                        dpg.add_input_text(tag="passphrase", default_value=load_config().get('passphrase', ''), password=True, width=300)
                dpg.bind_item_theme(dpg.last_item(), table_theme)
                dpg.add_button(label="保存 API 密钥", callback=lambda: (
                    save_api(),
                    threading.Thread(target=lambda: button_animation(dpg.last_item()), daemon=True).start()
                ))
                dpg.bind_item_theme(dpg.last_item(), button_theme)

            dpg.add_separator()
            # 实时监控
            with dpg.group():
                dpg.add_text("实时监控")
                dpg.bind_item_theme(dpg.last_item(), section_theme)
                with dpg.table(header_row=False, borders_outerV=True, borders_innerV=True, borders_outerH=True, borders_innerH=True):
                    dpg.add_table_column()
                    dpg.add_table_column()
                    with dpg.table_row():
                        dpg.add_text("BTC 价格")
                        dpg.add_text("当前 BTC 价格: $0.00", tag="price_label")
                    with dpg.table_row():
                        dpg.add_text("账户余额")
                        dpg.add_text("余额: 0.00 USDT, 多仓: 0 张, 空仓: 0 张", tag="balance_label")
                    with dpg.table_row():
                        dpg.add_text("RSI(14)")
                        dpg.add_text("RSI(14): 无数据", tag="rsi_display")
                    with dpg.table_row():
                        dpg.add_text("MACD")
                        dpg.add_text("MACD(12,26,9): 无数据", tag="macd_display")
                    with dpg.table_row():
                        dpg.add_text("信号线")
                        dpg.add_text("信号线(9): 无数据", tag="signal_display")
                    with dpg.table_row():
                        dpg.add_text("柱状图")
                        dpg.add_text("柱状图: 无数据", tag="histogram_display")
                dpg.bind_item_theme(dpg.last_item(), table_theme)
                if title_font:
                    dpg.bind_item_font("price_label", title_font)
                if body_font:
                    dpg.bind_item_font("balance_label", body_font)
                    dpg.bind_item_font("rsi_display", body_font)
                    dpg.bind_item_font("macd_display", body_font)
                    dpg.bind_item_font("signal_display", body_font)
                    dpg.bind_item_font("histogram_display", body_font)

            dpg.add_separator()
            # 交易设置选项卡
            with dpg.tab_bar():
                with dpg.tab(label="RSI 设置"):
                    with dpg.group():
                        dpg.add_checkbox(label="启用 RSI 交易", tag="rsi_enabled")
                        with dpg.table(header_row=False, borders_outerV=True, borders_innerV=True, borders_outerH=True, borders_innerH=True):
                            dpg.add_table_column()
                            dpg.add_table_column()
                            with dpg.table_row():
                                dpg.add_text("开仓比例 (%)")
                                dpg.add_input_float(tag="rsi_buy_ratio", default_value=10.0, min_value=0.0, max_value=100.0, width=150)
                            with dpg.table_row():
                                dpg.add_text("时间周期")
                                dpg.add_combo(tag="rsi_timeframe", items=list(timeframe_display_map.keys()), default_value="1小时", width=150)
                            with dpg.table_row():
                                dpg.add_text("开多 RSI")
                                dpg.add_input_float(tag="rsi_buy_value", default_value=30.0, min_value=0.0, max_value=100.0, width=150)
                            with dpg.table_row():
                                dpg.add_text("开空 RSI")
                                dpg.add_input_float(tag="rsi_sell_value", default_value=70.0, min_value=0.0, max_value=100.0, width=150)
                            with dpg.table_row():
                                dpg.add_text("杠杆倍数")
                                dpg.add_input_float(tag="rsi_leverage", default_value=3.0, min_value=1.0, max_value=10.0, width=150)
                            with dpg.table_row():
                                dpg.add_text("保证金模式")
                                dpg.add_combo(tag="rsi_margin_mode", items=['全仓', '逐仓'], default_value='全仓', width=150)
                            with dpg.table_row():
                                dpg.add_text("止盈比例 (%)")
                                dpg.add_input_float(tag="rsi_take_profit", default_value=10.0, min_value=0.0, max_value=100.0, width=150)
                            with dpg.table_row():
                                dpg.add_text("止损比例 (%)")
                                dpg.add_input_float(tag="rsi_stop_loss", default_value=5.0, min_value=0.0, max_value=100.0, width=150)
                            with dpg.table_row():
                                dpg.add_text("最大持仓 (%)")
                                dpg.add_input_float(tag="rsi_max_position_ratio", default_value=50.0, min_value=0.0, max_value=100.0, width=150)
                            with dpg.table_row():
                                dpg.add_text("交易方向")
                                dpg.add_combo(tag="rsi_trade_direction", items=['双向', '仅多头', '仅空头'], default_value='双向', width=150)
                            with dpg.table_row():
                                dpg.add_text("冷却倍数")
                                dpg.add_input_float(tag="rsi_cooldown_multiplier", default_value=1.0, min_value=0.5, max_value=5.0, width=150)
                            with dpg.table_row():
                                dpg.add_text("止盈止损方式")
                                dpg.add_combo(tag="rsi_tp_sl_mode", items=['交易所设定', '程序计算'], default_value='交易所设定', width=150)
                        dpg.bind_item_theme(dpg.last_item(), table_theme)

                with dpg.tab(label="MACD 设置"):
                    with dpg.group():
                        dpg.add_checkbox(label="启用 MACD 交易", tag="macd_enabled")
                        with dpg.table(header_row=False, borders_outerV=True, borders_innerV=True, borders_outerH=True, borders_innerH=True):
                            dpg.add_table_column()
                            dpg.add_table_column()
                            with dpg.table_row():
                                dpg.add_text("开仓比例 (%)")
                                dpg.add_input_float(tag="macd_buy_ratio", default_value=10.0, min_value=0.0, max_value=100.0, width=150)
                            with dpg.table_row():
                                dpg.add_text("时间周期")
                                dpg.add_combo(tag="macd_timeframe", items=list(timeframe_display_map.keys()), default_value="1小时", width=150)
                            with dpg.table_row():
                                dpg.add_text("杠杆倍数")
                                dpg.add_input_float(tag="macd_leverage", default_value=3.0, min_value=1.0, max_value=10.0, width=150)
                            with dpg.table_row():
                                dpg.add_text("保证金模式")
                                dpg.add_combo(tag="macd_margin_mode", items=['全仓', '逐仓'], default_value='全仓', width=150)
                            with dpg.table_row():
                                dpg.add_text("止盈比例 (%)")
                                dpg.add_input_float(tag="macd_take_profit", default_value=10.0, min_value=0.0, max_value=100.0, width=150)
                            with dpg.table_row():
                                dpg.add_text("止损比例 (%)")
                                dpg.add_input_float(tag="macd_stop_loss", default_value=5.0, min_value=0.0, max_value=100.0, width=150)
                            with dpg.table_row():
                                dpg.add_text("最大持仓 (%)")
                                dpg.add_input_float(tag="macd_max_position_ratio", default_value=50.0, min_value=0.0, max_value=100.0, width=150)
                            with dpg.table_row():
                                dpg.add_text("交易方向")
                                dpg.add_combo(tag="macd_trade_direction", items=['双向', '仅多头', '仅空头'], default_value='双向', width=150)
                            with dpg.table_row():
                                dpg.add_text("冷却倍数")
                                dpg.add_input_float(tag="macd_cooldown_multiplier", default_value=1.0, min_value=0.5, max_value=5.0, width=150)
                            with dpg.table_row():
                                dpg.add_text("止盈止损方式")
                                dpg.add_combo(tag="macd_tp_sl_mode", items=['交易所设定', '程序计算'], default_value='交易所设定', width=150)
                        dpg.bind_item_theme(dpg.last_item(), table_theme)

            dpg.add_separator()
            # 操作按钮
            with dpg.group(horizontal=True):
                dpg.add_button(label="保存设置", callback=lambda: (
                    save_settings(),
                    threading.Thread(target=lambda: button_animation(dpg.last_item()), daemon=True).start()
                ))
                dpg.bind_item_theme(dpg.last_item(), button_theme)
                dpg.add_button(label="启动交易", callback=lambda: (
                    start_trading(),
                    threading.Thread(target=lambda: button_animation(dpg.last_item()), daemon=True).start()
                ))
                dpg.bind_item_theme(dpg.last_item(), button_theme)
                dpg.add_button(label="停止交易", callback=lambda: (
                    stop_trading(),
                    threading.Thread(target=lambda: button_animation(dpg.last_item()), daemon=True).start()
                ))
                dpg.bind_item_theme(dpg.last_item(), button_theme)

            dpg.add_separator()
            # 状态显示
            with dpg.group():
                dpg.add_text("状态: 未运行", tag="status_label")
                if title_font:
                    dpg.bind_item_font(dpg.last_item(), title_font)

            # 帮助按钮
            dpg.add_button(label="？帮助", callback=lambda: (
                show_help(),
                threading.Thread(target=lambda: button_animation(dpg.last_item()), daemon=True).start()
            ), pos=(706, 45))
            dpg.bind_item_theme(dpg.last_item(), button_theme)

        dpg.setup_dearpygui()
        dpg.show_viewport()
        logging.info("DearPyGui viewport active")
        while dpg.is_dearpygui_running():
            update_ui_callback()
            dpg.render_dearpygui_frame()
        dpg.destroy_context()
        logging.info("DearPyGui context closed")
    except Exception as e:
        logging.error(f"GUI setup failed: {str(e)}\n{traceback.format_exc()}")
        raise

def main():
    try:
        # 加载配置文件并尝试自动初始化
        config = load_config()
        if config.get('api_key') and config.get('api_secret') and config.get('passphrase'):
            logging.info("检测到配置文件，尝试自动初始化 API")
            api_key = config.get('api_key')
            api_secret = config.get('api_secret')
            passphrase = config.get('passphrase')
            for flag in ['0', '1']:
                if init_okx(api_key, api_secret, passphrase, flag):
                    threading.Thread(target=start_websocket, args=(api_key, api_secret, passphrase), daemon=True).start()
                    timeout = 10
                    start_time = time.time()
                    while time.time() - start_time < timeout and not ws_connected:
                        time.sleep(1)
                    if ws_connected:
                        logging.info(f"自动初始化成功，WebSocket 已连接 (flag={flag})")
                        usdt, long_qty, short_qty, _, _, _ = get_balance()
                        if usdt is not None:
                            update_queue.put(("balance_label", f"余额: {usdt:.2f} USDT, 多仓: {long_qty:.0f} 张, 空仓: {short_qty:.0f} 张", (255, 255, 255)))
                        else:
                            update_queue.put(("balance_label", "余额: 获取失败", (255, 0, 0)))
                        # 初始化价格
                        price = get_btc_price()
                        if price:
                            with lock:
                                global current_price, price_update_time
                                current_price = price
                                price_update_time = datetime.now()
                            update_queue.put(("price_label", f"当前 BTC 价格: ${price:.2f}", (255, 255, 255)))
                            logging.info(f"自动初始化价格: ${price:.2f}")
                        break
                    else:
                        logging.warning(f"自动初始化 WebSocket 失败 (flag={flag})")
                else:
                    logging.error(f"自动初始化 API 失败 (flag={flag})")
        create_gui()
    except Exception as e:
        logging.error(f"主程序错误: {str(e)}\n{traceback.format_exc()}")
        sys.exit(1)

if __name__ == "__main__":
    main()