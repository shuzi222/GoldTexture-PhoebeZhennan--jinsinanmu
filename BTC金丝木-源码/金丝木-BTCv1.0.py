from binance.spot import Spot
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

# API 密钥存储文件
CONFIG_FILE = 'binance_config.json'

# 全局变量
client = None
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


# 加载或保存 API 密钥
def load_config():
    if os.path.exists(CONFIG_FILE):
        if os.path.getsize(CONFIG_FILE) == 0:
            return {}
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if not content:
                    return {}
                return json.loads(content)
        except json.JSONDecodeError:
            return {}
        except Exception:
            return {}
    return {}


def save_config(api_key, api_secret):
    config = {'api_key': api_key, 'api_secret': api_secret}
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception:
        pass


# 初始化 Binance API
def init_binance(api_key, api_secret):
    global client
    try:
        client = Spot(api_key=api_key, api_secret=api_secret, base_url='https://api.binance.com')
        client.time()
        return True
    except Exception:
        return False


# 获取 BTC 当前价格
def get_btc_price():
    global current_price, price_update_time
    try:
        ticker = client.ticker_price(symbol='BTCUSDT')
        with lock:
            current_price = float(ticker['price'])
            price_update_time = datetime.now()
        return current_price
    except Exception:
        return None


# 获取 K 线数据
def get_klines(symbol, interval, limit=200):
    try:
        klines = client.klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_asset_volume', 'trades', 'taker_buy_base', 'taker_buy_quote', 'ignored'
        ])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df['close'] = df['close'].astype(float)
        if df['close'].isna().any():
            df['close'] = df['close'].fillna(method='ffill')
        return df
    except Exception:
        return None


# 计算 RSI
def calculate_rsi(df, period=14):
    if len(df) < period + 1:
        return None
    close = df['close']
    rsi = talib.RSI(close, timeperiod=period)
    return rsi


# 计算 MACD
def calculate_macd(df, fast=12, slow=26, signal=9):
    min_length = slow + signal - 1
    if len(df) < min_length:
        return None, None, None
    close = df['close']
    macd, signal_line, histogram = talib.MACD(close, fastperiod=fast, slowperiod=slow, signalperiod=signal)
    return macd, signal_line, histogram


# 获取交易对信息
def get_symbol_info(symbol='BTCUSDT'):
    try:
        info = client.get_symbol_info(symbol)
        quantity_precision = info['quantityPrecision']
        min_qty = float(next(filter(lambda x: x['filterType'] == 'LOT_SIZE', info['filters']))['minQty'])
        return quantity_precision, min_qty
    except Exception:
        return 8, 0.0001


# 下单函数
def place_order(side, quantity):
    try:
        quantity_precision, min_qty = get_symbol_info('BTCUSDT')
        quantity = round(quantity, quantity_precision)
        if quantity < min_qty:
            update_queue.put(("status_label", f"下单失败: 数量 {quantity} 小于最小交易量 {min_qty}"))
            return None
        params = {
            'symbol': 'BTCUSDT',
            'side': side.upper(),
            'type': 'MARKET',
            'quantity': f"{quantity:.{quantity_precision}f}"
        }
        order = client.new_order(**params)
        return order
    except Exception as e:
        update_queue.put(("status_label", f"下单失败: {str(e)}"))
        return None


# 获取账户余额
def get_balance():
    try:
        account = client.account()
        usdt = float(next(asset for asset in account['balances'] if asset['asset'] == 'USDT')['free'])
        btc = float(next(asset for asset in account['balances'] if asset['asset'] == 'BTC')['free'])
        return usdt, btc
    except Exception:
        return None, None


# RSI 交易逻辑
def rsi_trading():
    global last_rsi_buy_time, last_rsi_sell_time, latest_rsi
    if not rsi_enabled:
        return
    df = get_klines('BTCUSDT', rsi_timeframe, limit=200)
    if df is None:
        return
    rsi = calculate_rsi(df)
    if rsi is None:
        return
    latest_rsi_value = rsi.iloc[-1]
    if pd.isna(latest_rsi_value):
        return
    with lock:
        latest_rsi = latest_rsi_value
    usdt_balance, btc_balance = get_balance()
    if usdt_balance is None or btc_balance is None:
        return
    with lock:
        if current_price <= 0:
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
        if (last_rsi_buy_time is None or (now - last_rsi_buy_time) >= cooldown):
            if latest_rsi <= rsi_buy_value:
                quantity = (usdt_balance * rsi_buy_ratio / 100) / current_price
                if quantity <= 0:
                    update_queue.put(("status_label", "RSI 买入失败: 余额不足"))
                    return
                order = place_order('buy', quantity)
                if order:
                    last_rsi_buy_time = now
                    update_queue.put(("status_label", f"RSI 买入成功: 数量 {quantity:.6f} BTC"))
        if (last_rsi_sell_time is None or (now - last_rsi_sell_time) >= cooldown):
            if latest_rsi >= rsi_sell_value and btc_balance > 0:
                order = place_order('sell', btc_balance)
                if order:
                    last_rsi_sell_time = now
                    update_queue.put(("status_label", f"RSI 卖出成功: 数量 {btc_balance:.6f} BTC"))


# MACD 交易逻辑
def macd_trading():
    global last_macd_buy_time, last_macd_sell_time, latest_macd, latest_signal, latest_histogram
    if not macd_enabled:
        return
    df = get_klines('BTCUSDT', macd_timeframe, limit=200)
    if df is None:
        return
    macd, signal_line, histogram = calculate_macd(df)
    if macd is None or signal_line is None or histogram is None:
        return
    latest_macd_value = macd.iloc[-1]
    latest_signal_value = signal_line.iloc[-1]
    latest_histogram_value = histogram.iloc[-1]
    prev_macd = macd.iloc[-2]
    prev_signal = signal_line.iloc[-2]
    if any(pd.isna(x) for x in [latest_macd_value, prev_macd, latest_signal_value, prev_signal]):
        return
    with lock:
        latest_macd = latest_macd_value
        latest_signal = latest_signal_value
        latest_histogram = latest_histogram_value
    usdt_balance, btc_balance = get_balance()
    if usdt_balance is None or btc_balance is None:
        return
    with lock:
        if current_price <= 0:
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
        if (last_macd_buy_time is None or (now - last_macd_buy_time) >= cooldown):
            if latest_macd < 0 and prev_macd < prev_signal and latest_macd > latest_signal:
                quantity = (usdt_balance * macd_buy_ratio / 100) / current_price
                if quantity <= 0:
                    update_queue.put(("status_label", "MACD 买入失败: 余额不足"))
                    return
                order = place_order('buy', quantity)
                if order:
                    last_macd_buy_time = now
                    update_queue.put(("status_label", f"MACD 买入成功: 数量 {quantity:.6f} BTC"))
        if (last_macd_sell_time is None or (now - last_macd_sell_time) >= cooldown):
            if latest_macd > 0 and prev_macd > prev_signal and latest_macd < latest_signal and btc_balance > 0:
                order = place_order('sell', btc_balance)
                if order:
                    last_macd_sell_time = now
                    update_queue.put(("status_label", f"MACD 卖出成功: 数量 {btc_balance:.6f} BTC"))


# 主交易循环
def trading_loop():
    global running, animation_frame
    interval_seconds = 10
    if rsi_timeframe in ['1m', '5m'] or macd_timeframe in ['1m', '5m']:
        interval_seconds = 5

    while running:
        try:
            price = get_btc_price()
            if not price:
                with lock:
                    if price_update_time and (datetime.now() - price_update_time) > timedelta(minutes=5):
                        update_queue.put(
                            ("price_label", f"当前 BTC 价格: ${current_price:.2f} (数据可能已过期)", (255, 165, 0)))
                    else:
                        animation_frame += 1
                        color = (255, 255, 0) if animation_frame % 20 < 10 else (0, 255, 255)
                        update_queue.put(("price_label", f"当前 BTC 价格: ${current_price:.2f}", color))
                continue

            animation_frame += 1
            color = (255, 255, 0) if animation_frame % 20 < 10 else (0, 255, 255)
            update_queue.put(("price_label", f"当前 BTC 价格: ${price:.2f}", color))

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
        except Exception:
            pass
        time.sleep(interval_seconds)


# 界面更新回调
def update_ui_callback():
    while not update_queue.empty():
        item = update_queue.get()
        tag = item[0]
        value = item[1]
        color = item[2] if len(item) > 2 else None
        try:
            if value is not None:
                dpg.set_value(tag, value)
            if color is not None:
                dpg.configure_item(tag, color=color)
        except Exception:
            pass

    if running:
        with lock:
            if latest_rsi is not None:
                rsi_text = f"RSI(14): {latest_rsi:.2f}"
                rsi_color = (255, 0, 0) if latest_rsi >= rsi_sell_value else (
                0, 255, 0) if latest_rsi <= rsi_buy_value else (255, 255, 255)
                dpg.set_value("rsi_display", rsi_text)
                dpg.configure_item("rsi_display", color=rsi_color)
            else:
                dpg.set_value("rsi_display", "RSI(14): N/A")
                dpg.configure_item("rsi_display", color=(255, 255, 255))

            if latest_macd is not None:
                macd_text = f"MACD(12,26,9): {latest_macd:.2f}"
                macd_color = (0, 255, 0) if latest_macd > 0 else (255, 0, 0) if latest_macd < 0 else (255, 255, 255)
                dpg.set_value("macd_display", macd_text)
                dpg.configure_item("macd_display", color=macd_color)
            else:
                dpg.set_value("macd_display", "MACD(12,26,9): N/A")
                dpg.configure_item("macd_display", color=(255, 255, 255))

            if latest_signal is not None:
                signal_text = f"Signal(9): {latest_signal:.2f}"
                signal_color = (255, 255, 0)
                dpg.set_value("signal_display", signal_text)
                dpg.configure_item("signal_display", color=signal_color)
            else:
                dpg.set_value("signal_display", "Signal(9): N/A")
                dpg.configure_item("signal_display", color=(255, 255, 255))

            if latest_histogram is not None:
                hist_text = f"Histogram: {latest_histogram:.2f}"
                hist_color = (0, 255, 0) if latest_histogram > 0 else (255, 0, 0) if latest_histogram < 0 else (
                255, 255, 255)
                dpg.set_value("histogram_display", hist_text)
                dpg.configure_item("histogram_display", color=hist_color)
            else:
                dpg.set_value("histogram_display", "Histogram: N/A")
                dpg.configure_item("histogram_display", color=(255, 255, 255))


# 启动交易
def start_trading():
    global running
    if not client:
        dpg.set_value("status_label", "请先输入有效的 API 密钥")
        return
    running = True
    threading.Thread(target=trading_loop, daemon=True).start()
    dpg.set_value("status_label", "交易已启动")


# 停止交易
def stop_trading():
    global running
    running = False
    dpg.set_value("status_label", "交易已停止")


# 保存配置
def save_settings():
    global rsi_enabled, macd_enabled, rsi_buy_ratio, macd_buy_ratio, rsi_timeframe, macd_timeframe, rsi_buy_value, rsi_sell_value
    rsi_enabled = dpg.get_value("rsi_enabled")
    macd_enabled = dpg.get_value("macd_enabled")
    rsi_buy_ratio = dpg.get_value("rsi_buy_ratio")
    macd_buy_ratio = dpg.get_value("macd_buy_ratio")
    rsi_timeframe = dpg.get_value("rsi_timeframe")
    macd_timeframe = dpg.get_value("macd_timeframe")
    rsi_buy_value = dpg.get_value("rsi_buy_value")
    rsi_sell_value = dpg.get_value("rsi_sell_value")
    dpg.set_value("status_label", "设置已保存")


# 保存 API 密钥
def save_api():
    api_key = dpg.get_value("api_key")
    api_secret = dpg.get_value("api_secret")
    if init_binance(api_key, api_secret):
        save_config(api_key, api_secret)
        dpg.set_value("status_label", "API 密钥已保存")
    else:
        dpg.set_value("status_label", "无效的 API 密钥")


# 按钮点击动画回调
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


# 显示说明书窗口
def show_help_window():
    if dpg.does_item_exist("help_window"):
        dpg.delete_item("help_window")

    with dpg.window(label="使用说明书", tag="help_window", width=600, height=400, pos=(100, 100), no_scrollbar=False):
        dpg.add_text("BTC 金丝木量化交易机器人 - 使用说明书", color=(0, 255, 255))
        dpg.add_separator()

        dpg.add_text("1. 交易逻辑", color=(255, 215, 0))
        dpg.add_text("本程序基于 RSI 和 MACD 指标进行自动化交易，适用于 BTC/USDT 交易对。")
        dpg.add_text("- RSI 交易逻辑：")
        dpg.add_text("  * 买入：当 RSI(14) ≤ 买入阈值（默认 30），且距离上次买入超过冷却时间。")
        dpg.add_text("  * 卖出：当 RSI(14) ≥ 卖出阈值（默认 70），且距离上次卖出超过冷却时间。")
        dpg.add_text("- MACD 交易逻辑：")
        dpg.add_text("  * 买入：当 MACD 线 < 0 且 MACD 线从下方穿过信号线（黄叉）。")
        dpg.add_text("  * 卖出：当 MACD 线 > 0 且 MACD 线从上方穿过信号线（死叉）。")
        dpg.add_separator()

        dpg.add_text("2. 使用方式", color=(255, 215, 0))
        dpg.add_text("步骤：")
        dpg.add_text("1) 在 'Binance API 设置' 中输入你的 API Key 和 API Secret，点击 '保存 API 密钥'。")
        dpg.add_text("   - 确保 API 密钥具有交易权限。")
        dpg.add_text("2) 在 'RSI 交易设置' 和 'MACD 交易设置' 中配置参数：")
        dpg.add_text("   - 勾选 '启用 RSI 交易' 或 '启用 MACD 交易' 以激活相应策略。")
        dpg.add_text("   - 设置单次买入比例（%）：决定每次买入时使用多少 USDT 余额。")
        dpg.add_text("   - 选择时间周期：决定指标计算的 K 线周期（如 1m、1h、1d）。")
        dpg.add_text("   - 设置 RSI 买入/卖出阈值：调整 RSI 交易的触发条件。")
        dpg.add_text("3) 点击 '保存设置' 确认参数！(一定不要遗漏)")
        dpg.add_text("4) 点击 '启动交易' 开始自动化交易。")
        dpg.add_text("5) 点击 '停止交易' 暂停交易。")
        dpg.add_text("6) 查看 '指标实时监控' 部分，了解当前的 RSI 和 MACD 指标值。")
        dpg.add_text("注意事项：")
        dpg.add_text("- 确保账户有足够的 USDT 和 BTC 余额以执行交易。")
        dpg.add_text("- 交易日志已移除，运行状态仅通过界面状态栏显示。")
        dpg.add_separator()

        dpg.add_text("3. 动态冷却时间", color=(255, 215, 0))
        dpg.add_text("为了避免过于频繁的交易，程序为 RSI 和 MACD 交易设置了动态冷却时间，具体如下：")
        dpg.add_text("- 1m（1分钟）：冷却时间 5 分钟")
        dpg.add_text("- 5m（5分钟）：冷却时间 15 分钟")
        dpg.add_text("- 15m（15分钟）：冷却时间 30 分钟")
        dpg.add_text("- 30m（30分钟）：冷却时间 45 分钟")
        dpg.add_text("- 1h（1小时）：冷却时间 1 小时")
        dpg.add_text("- 4h（4小时）：冷却时间 4 小时")
        dpg.add_text("- 1d（1天）：冷却时间 1 天")
        dpg.add_text("作用：")
        dpg.add_text("- 冷却时间根据选择的时间周期自动调整，确保交易频率与策略周期匹配。")
        dpg.add_text("- 短时间周期（如 1m）适合高频交易，冷却时间短，交易更频繁。")
        dpg.add_text("- 长时间周期（如 1d）适合趋势交易，冷却时间长，交易频率低。")
        dpg.add_separator()

        dpg.add_text("想支持树酱的话可以向以下钱包地址捐款：", color=(128, 128, 128))
        dpg.add_text("0x4FdFCfc03A5416EB5d9B85F4bad282e6DaC19783", color=(128, 128, 128))
        dpg.add_text("感谢你的支持呀（不捐也没关系，作者会自己找垃圾吃的", color=(128, 128, 128))


# DearPyGui 界面
def create_gui():
    dpg.create_context()
    icon_path = os.path.join(os.path.dirname(__file__), "jio.ico")
    if not os.path.exists(icon_path):
        print(f"错误：图标文件 {icon_path} 不存在，请确保文件放置正确")
    else:
        dpg.create_viewport(title='Tree Bot', width=750, height=850, small_icon=icon_path, large_icon=icon_path)

    font_path = os.path.join(os.path.dirname(__file__), "NotoSerifCJKsc-dick.otf")
    if not os.path.exists(font_path):
        print(f"错误：字体文件 {font_path} 不存在，请确保文件放置正确")
        return

    with dpg.font_registry():
        with dpg.font(font_path, 28) as title_font:
            dpg.add_font_range_hint(dpg.mvFontRangeHint_Chinese_Full)
        with dpg.font(font_path, 20) as body_font:
            dpg.add_font_range_hint(dpg.mvFontRangeHint_Chinese_Full)
        dpg.bind_font(body_font)

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

    dpg.bind_theme(global_theme)

    with dpg.window(label="树酱提示：本软件完全免费开源，你从任何途径购买都说明被骗了", width=850, height=850, pos=(0, 0), no_scrollbar=True):
        with dpg.group():
            title_text = "BTC 金丝木量化交易（不知道为什么我窗口标题汉字老是乱码，所以移到这里来了）"
            title_width = len(title_text) * 18
            with dpg.drawlist(width=title_width, height=40):
                for i in range(title_width):
                    t = i / title_width
                    r = int(255 * (1 - t))
                    g = int(215 + 40 * t)
                    b = int(0 + 255 * t)
                    dpg.draw_line((i, 0), (i, 40), color=(r, g, b, 255))
                dpg.draw_text((0, 0), title_text, size=28)
            dpg.bind_item_font(dpg.last_item(), title_font)
            dpg.add_separator()

            with dpg.group():
                dpg.add_text("Binance API 设置")
                dpg.bind_item_theme(dpg.last_item(), section_theme)
                with dpg.group(horizontal=True):
                    dpg.add_input_text(label="API Key", tag="api_key", default_value=load_config().get('api_key', ''),
                                       width=600)
                with dpg.group(horizontal=True):
                    dpg.add_input_text(label="API Secret", tag="api_secret",
                                       default_value=load_config().get('api_secret', ''), password=True, width=600)
                dpg.add_button(label="保存 API 密钥", callback=lambda: (
                save_api(), threading.Thread(target=button_animation, args=(dpg.last_item(),)).start()))
                dpg.bind_item_theme(dpg.last_item(), button_theme)

            dpg.add_separator()

            with dpg.group():
                dpg.add_text("当前 BTC 价格: $0.00", tag="price_label")
                dpg.bind_item_font(dpg.last_item(), title_font)

            dpg.add_separator()

            with dpg.group():
                dpg.add_text("指标实时监控", color=(0, 255, 255))
                dpg.bind_item_font(dpg.last_item(), title_font)
                dpg.add_separator()
                dpg.add_text("RSI(14): N/A", tag="rsi_display")
                dpg.bind_item_font(dpg.last_item(), body_font)
                dpg.add_text("MACD(12,26,9): N/A", tag="macd_display")
                dpg.bind_item_font(dpg.last_item(), body_font)
                dpg.add_text("MACD-信号(9): N/A", tag="signal_display")
                dpg.bind_item_font(dpg.last_item(), body_font)
                dpg.add_text("柱状图: N/A", tag="histogram_display")
                dpg.bind_item_font(dpg.last_item(), body_font)

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
                                  items=['1m', '5m', '15m', '30m', '1h', '4h', '1d'], default_value='1h', width=200)
                with dpg.group(horizontal=True):
                    dpg.add_input_float(label="买入 RSI 值", tag="rsi_buy_value", default_value=30.0, min_value=0.0,
                                        max_value=100.0, width=200)
                with dpg.group(horizontal=True):
                    dpg.add_input_float(label="卖出 RSI 值", tag="rsi_sell_value", default_value=70.0, min_value=0.0,
                                        max_value=100.0, width=200)

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
                                  items=['1m', '5m', '15m', '30m', '1h', '4h', '1d'], default_value='1h', width=200)

            dpg.add_separator()

            with dpg.group(horizontal=True):
                dpg.add_button(label="保存设置", callback=lambda: (
                save_settings(), threading.Thread(target=button_animation, args=(dpg.last_item(),)).start()))
                dpg.bind_item_theme(dpg.last_item(), button_theme)
                dpg.add_button(label="启动交易", callback=lambda: (
                start_trading(), threading.Thread(target=button_animation, args=(dpg.last_item(),)).start()))
                dpg.bind_item_theme(dpg.last_item(), button_theme)
                dpg.add_button(label="停止交易", callback=lambda: (
                stop_trading(), threading.Thread(target=button_animation, args=(dpg.last_item(),)).start()))
                dpg.bind_item_theme(dpg.last_item(), button_theme)

            dpg.add_separator()

            with dpg.group():
                dpg.add_text("状态: 未启动", tag="status_label")
                dpg.bind_item_font(dpg.last_item(), title_font)

            # 添加问号按钮，位置在右下角
            dpg.add_button(label="？", callback=lambda: (
            show_help_window(), threading.Thread(target=button_animation, args=(dpg.last_item(),)).start()),
                           pos=(600, 720))
            dpg.bind_item_theme(dpg.last_item(), button_theme)

    dpg.setup_dearpygui()
    dpg.show_viewport()
    while dpg.is_dearpygui_running():
        update_ui_callback()
        dpg.render_dearpygui_frame()
    dpg.destroy_context()


# 主函数
def main():
    config = load_config()
    if config.get('api_key') and config.get('api_secret'):
        init_binance(config['api_key'], config['api_secret'])
    create_gui()


if __name__ == "__main__":
    main()