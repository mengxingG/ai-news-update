# AI News Radar API ( mengxing-ai.it.com 专属后端 )

基于 `last30days-skill` 核心打分引擎二次开发的独立微服务。
作为 [mengxing-ai.it.com](https://www.mengxing-ai.it.com/ai-news) 的后端数据抓取节点，配合 Make.com + Notion 自动化工作流运行。

## 🚀 核心改造说明
- **剥离 CLI 交互**：封装为标准 FastAPI 接口，直接返回 JSON 数组。
- **优雅降级**：跳过需要繁琐授权的 X (Twitter)，专注抓取 Hacker News、Polymarket、Reddit 和 YouTube。
- **时间维度分层**：支持按天数筛选，完美适配“日更”、“周榜”、“月榜”业务需求。

## 🛠️ 本地启动指南

1. 安装项目依赖：
   ```bash
   uv sync

2. 启动本地 API 服务：

Bash
uv run uvicorn server:app --reload

3. 服务地址：http://127.0.0.1:8000

📡 API 接口文档
GET /api/news
触发多源情报抓取并返回排名最高的高质量内容。

Query Parameters:

topic (string, 必填): 搜索关键词，例如 AI Agent, LLM

days (integer, 选填, 默认 1): 溯源时间范围。1 代表今明两天，7 代表近一周。

Request Example:

HTTP
GET /api/news?topic=AI+Agent&days=7
Response Example:

JSON
[
  {
    "Title": "AI agent in a robot does exactly what experts warned",
    "Source": "YouTube",
    "Author": "InsideAI",
    "OriginalText": "Highlights: 20 researchers gave the agents access to...",
    "Date": "2026-04-09",
    "URL": "[https://www.youtube.com/watch?v=](https://www.youtube.com/watch?v=)..."
  }
]

☁️ 部署说明
本项目已配置好完整的 pyproject.toml，可直接通过 Zeabur、Render 或 Railway 等 PaaS 平台一键部署为 Python 服务。启动命令默认使用 Uvicorn。