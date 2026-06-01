# AI News Radar · Engine A（社区共识引擎）

> 为个人网站 [mengxing-ai.it.com](https://www.mengxing-ai.it.com) 提供 **Engine A** 深度采集能力。
> 与 [my-ai-portfolio](https://github.com/mengxingG/my-ai-portfolio) 的 Next.js 前端、Notion CMS、
> 飞书 ChatOps 网关协同，构成完整的 **AI News Radar** 资讯闭环。

[![Deploy](https://img.shields.io/badge/Deploy-Render-46e3b7)](https://render.com)
[![Python](https://img.shields.io/badge/Python-3.12-blue)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-latest-009485)](https://fastapi.tiangolo.com)

## 项目定位

本项目是 AI News Radar **双引擎架构**中的 **Engine A（社区共识引擎）**：

| | Engine A（本项目） | Engine B（Coze 工作流） |
|---|---|---|
| 技术栈 | Python + FastAPI + Render | Coze 可视化工作流 |
| 数据源 | Hacker News / Polymarket / YouTube 字幕 | X 博主 / 中文媒体 / RSS |
| 触发时间 | 每天 09:30（Make.com 或本地 `fetch-news`） | 每天 10:42 |
| 优势 | 深度、可迭代、稳定 | 灵活、快速、广覆盖 |

两个引擎写入**同一个 Notion 资讯库**，由 `my-ai-portfolio` 统一渲染为 Web 三视图与飞书卡片。

### 与 my-ai-portfolio 的分工

| 层级 | 仓库 | 职责 |
|------|------|------|
| **采集 + 翻译** | `ai-news-update`（本仓库） | HN / Polymarket / YouTube 三源检索 → DeepSeek/Gemini 中文化 → JSON |
| **入库** | `my-ai-portfolio` | `npm run fetch-news` 调用 `/api/news` → 北京时区今/昨过滤 → URL 去重写 Notion |
| **Web 阅读** | `my-ai-portfolio` | `/ai-news` 三视图：精选 / 全部（Notion）+ AI 日报（AI HOT 官方 API） |
| **飞书触达** | `my-ai-portfolio` + `job_engine` | 6 条菜单暗号 → Python 门卫 → Node `:3001` 卡片引擎 |

> **说明**：Web「AI 日报」Tab 仍直连 AI HOT 官方日报 API，不经本引擎；本引擎负责 **精选入库** 的社区共识信号。

## 项目效果

![AI News Dashboard](./images/news-dashboard.png)

![AI News Page](./images/news-page.png)

线上体验：https://www.mengxing-ai.it.com/ai-news

## 系统架构与数据流转

![系统架构](./images/image.png)

**四阶段闭环**（与 my-ai-portfolio README 一致）：

1. **分布式采集**：Engine A（09:30）与 Engine B（10:42）错峰唤醒，降低 API 限流概率
2. **清洗与规范化**：格式化为 JSON Array（`Title`, `Source`, `Author`, `URL`, `OriginalText`, `Date`, `TimeRange`）
3. **入库调度**：`my-ai-portfolio` 的 `lib/cron-fetch-news.ts`（或 Make.com）执行去重，统一写入 Notion
4. **前端渲染**：Next.js 直连 Notion API；飞书菜单经 `feishu_gateway.py` 转发 Node 卡片引擎

```
Engine A (本仓库 FastAPI)
  └─ GET /api/news?topic=AI&days=1
        └─ my-ai-portfolio: npm run fetch-news
              └─ Notion 资讯库
                    ├─ /ai-news 精选 · 全部
                    ├─ 首页 AINewsWidget（最多 20 条）
                    └─ 飞书 6 菜单 → Node 卡片（仍读 AI HOT 日报/分类 API）
```

## 项目来源

基于开源项目 [mvanhorn/last30days-skill](https://github.com/mvanhorn/last30days-skill) 改造：

- **微服务化**：FastAPI 封装 CLI 管线，供 `my-ai-portfolio` / Make.com 定时调用
- **三源精简**：HN / Polymarket / YouTube（X 源由 Coze Engine B 承担）
- **Gemini → DeepSeek**：部分地区 Google API 不稳定，默认 DeepSeek OpenAI 兼容接口
- **优雅降级**：每源 60 秒超时，单源故障不拖死整条管线
- **JSON 输出**：结构化数组，字段与 Notion 资讯库列名一一对应

## 快速开始

### 本地运行 Engine A

```bash
git clone https://github.com/mengxingG/ai-news-update.git
cd ai-news-update

conda create -n ainews python=3.12
conda activate ainews
pip install -e ".[server]"

export DEEPSEEK_API_KEY=sk-xxx
export LLM_PROVIDER=deepseek

python server.py
# 健康检查
curl http://127.0.0.1:8000/health
# 拉取今日 AI 资讯（较慢，LLM 逐条翻译）
curl "http://127.0.0.1:8000/api/news?topic=AI&days=1"
```

### 与 my-ai-portfolio 联调入库

在 `my-ai-portfolio/.env.local` 中配置：

```bash
NOTION_API_KEY=
NOTION_AI_NEWS_DB_ID=

# Engine A 采集服务（本地或 Render 部署地址）
AI_NEWS_UPDATE_API_URL=http://127.0.0.1:8000
AI_NEWS_UPDATE_TOPIC=AI
AI_NEWS_UPDATE_DAYS=1
AI_NEWS_UPDATE_TIMEOUT_MS=180000
```

```bash
# 终端 1 — Engine A
cd ~/news/ai-news-update && python server.py

# 终端 2 — 入库 Notion
cd ~/my-ai-portfolio && npm run fetch-news

# 终端 3 — 前端预览
npm run dev
# 打开 http://localhost:3000/ai-news
```

### Render 部署

环境变量：

```
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-xxx
```

生产环境将 `my-ai-portfolio` 的 `AI_NEWS_UPDATE_API_URL` 指向 Render 服务地址即可。

### Make.com（可选）

若不使用 `npm run fetch-news`，可由 Make.com 定时调用：

- HTTP GET `https://your-service.onrender.com/api/news?topic=AI&days=1`
- Timeout: **180 秒**
- Parse response: ON → 写入 Notion（字段映射见下表）

| API 字段 | Notion 列 |
|----------|-----------|
| Title | Title |
| OriginalText | OriginalText |
| Source | Source |
| URL | URL |
| Date | Date |
| Author | Author（建议前缀 `Engine A ·`） |

## API 接口

### GET /api/news

**参数**：

- `topic`（必填）：搜索主题，例 `AI`
- `days`（可选）：回溯天数，默认 `1`，范围 1–366

**返回**：

```json
[
  {
    "Title": "DeepSeek 开源新版 R2 模型",
    "Source": "Hacker News",
    "Author": "dang",
    "URL": "https://news.ycombinator.com/item?id=xxx",
    "OriginalText": "DeepSeek 今日开源 R2 模型，在数学与代码基准上…",
    "Date": "2026-04-12",
    "TimeRange": "1d"
  }
]
```

### GET /health

```json
{
  "status": "ok",
  "llm_provider": "deepseek",
  "sources": "hackernews,polymarket,youtube"
}
```

## 关键设计决策

### 为什么从 Gemini 切换到 DeepSeek？

Google API 代理访问稳定性不佳，gRPC 连接经常超时。DeepSeek 提供 OpenAI 兼容接口 + 原生中文优势 + 极低成本，实测稳定 3 秒内返回。

### 为什么不用 X 官方 API？

xAI Console 新用户注册不再发放 $25 免费额度。X 数据抓取改由 Coze Engine B 通过 GoogleWebSearch 覆盖。

### 为什么要限制每源 60 秒超时？

YouTube 的 yt-dlp 偶尔会卡死 fork 子进程。60 秒硬超时 + 独立线程执行确保单源故障不阻塞主管线。

## 致谢

- [mvanhorn/last30days-skill](https://github.com/mvanhorn/last30days-skill) — 原始项目
- [my-ai-portfolio](https://github.com/mengxingG/my-ai-portfolio) — Web / Notion / 飞书集成
- [Anthropic Claude Code](https://claude.ai/code) — 协助迁移与调试
- [Cursor](https://cursor.sh) — IDE 与 Agent

## License

MIT
