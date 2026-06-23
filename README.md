# Telegram Web2 Bot

一个不托管私钥、不发起链上交易的 Web2 Telegram 机器人。它只做链上公开数据查询、自动记账、OTC 台账、价格/汇率查询和播报。

## 功能

- TRON 地址监控：TRC20 收支轮询，自动写入 SQLite 台账并推送到绑定群/私聊
- TRON 手动同步：导入指定地址最近链上收支
- TON NFT 查询：查询 TON 地址持有 NFT
- OTC 承兑记账：创建订单、记录收付款/放行/费用、查看未结订单和汇总
- 记账器：负责每一笔钱财的收入、支出、OTC 流水和下发金额换算
- 加减乘除：安全表达式计算
- 汇率与币价：法币汇率和加密货币实时价格
- Telegram 收藏用户名/+888 匿名号码：提供 Fragment/TON NFT 市场查询入口
- 当日巨鲸播报：按 TON NFT 转移数据生成今日大额成交观察摘要

## 启动

1. 复制环境文件：

```powershell
copy .env.example .env
```

2. 编辑 `.env`，填入：

```text
BOT_TOKEN=你的 Telegram Bot Token
```

3. 启动：

```powershell
.\start.ps1
```

也可以直接：

```powershell
python bot.py
```

本项目只使用 Python 标准库，不需要 `pip install`。

## 常用命令

```text
/help
/menu
/calc 100*7.2+88
/payout 1000 7.23 3
/price BTC ETH TRX TON
/rate USD CNY 100

/tron_watch Txxx label
/tron_list
/tron_unwatch Txxx
/tron_sync Txxx
/ledger
/ledger_add in USDT 100 客户入款

/ton_nfts EQxxx
/tg_asset @username
/tg_asset +88812345678
/whale_today

/otc_new 张三 buy USDT 1000 7.23 备注
/otc_pay 1 7230 客户付款
/otc_release 1 1000 已放币
/otc_fee 1 3 手续费
/otc_close 1
/otc_open
/otc_show 1
/otc_summary
```

发送 `/start`、`/help` 或 `/menu` 会显示按钮菜单。点击按钮后，按机器人提示输入信息即可开始查询、记账或换算下发金额。

## 部署

### Windows 常驻

用任务计划程序运行：

```text
C:\Users\123\telegram-web2-bot\start.ps1
```

### Docker

```powershell
docker build -t telegram-web2-bot .
docker run --env-file .env -v ${PWD}/data:/app/data telegram-web2-bot
```

## 重要说明

这个机器人不应收集用户私钥、助记词、交易所密码、OTP 或银行卡敏感信息。OTC 模块只做台账，不做资金托管、不做支付指令、不自动放币。
