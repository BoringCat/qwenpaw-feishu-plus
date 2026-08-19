# QwenPaw Feishu Plus

QwenPaw 飞书渠道增强插件（channel plugin）。继承内置 `FeishuChannel`，只覆写话题（thread）相关逻辑，为群聊话题场景补齐三项能力：

- **话题内审批卡片** —— tool-guard 工具审批卡片发送到话题内（而非会话根），按钮点击后 `/approval` 命令也回注到原话题；
- **话题内流式输出** —— CardKit 流式卡片在话题内创建与更新，失败自动回退纯文本；
- **命令消息跳过引用获取** —— 以 `/` 开头的消息不抓取被回复卡片的内容，避免 `[quoted interactive: ...]` 前缀导致命令匹配失效。

此外提供**话题级会话聚合**：话题内的群消息按 `thread_id` 聚合 session，同一话题共享一个会话上下文。

## 前置要求

| 项目 | 要求 |
| :--- | :--- |
| QwenPaw | 2.0.0 ~ 2.1.0 （在 2.0.1 与 2.1.0 上验证通过） |
| Python | 3.12 （当时 QwenPaw 1.1.12 安装脚本的默认版本） |
| Python 依赖 | `lark-oapi`（由插件声明，QwenPaw 安装插件时自动拉取） |

## 安装

1. 将本仓库克隆或复制到 QwenPaw 的插件目录（插件清单位于 `src/plugin.json`，入口为 `src/plugin.py`）：

   ```bash
   git clone <本仓库地址> qwenpaw-feishu-plus
   ```

2. 重启 QwenPaw（或按 QwenPaw 文档刷新插件），控制台的渠道列表中会出现独立的「飞书+」卡片，与内置「飞书」并列。

3. **停用内置「飞书」渠道。** 二者共用同一飞书应用时只能启用其一，否则事件会重复消费。

## 配置

在控制台「飞书+」渠道卡片中填写：

| 字段 | 类型 | 必填 | 默认 | 说明 |
| :--- | :--- | :--- | :--- | :--- |
| `app_id` | 密码 | ✅ | — | 飞书应用 App ID（`cli_xxxx`） |
| `app_secret` | 密码 | ✅ | — | 飞书应用 App Secret |
| `encrypt_key` | 密码 | — | — | 事件加密密钥（可选） |
| `verification_token` | 密码 | — | — | Verification Token（可选） |
| `domain` | 文本 | — | `feishu` | 国内用 `feishu`，海外用 `lark` |
| `media_dir` | 文本 | — | — | 媒体文件目录 |
| `streaming_enabled` | 开关 | — | 关闭 | 流式输出，需在飞书开放平台开通 `cardkit:card:write` 权限 |
| `share_session_in_group` | 开关 | — | 关闭 | 群内所有成员共享同一会话上下文 |
| `access_control_dm` | 开关 | — | 关闭 | 开启后仅白名单用户可私聊机器人 |
| `access_control_group` | 开关 | — | 关闭 | 开启后仅白名单用户可在群聊中与机器人互动 |
| `require_mention` | 开关 | — | 关闭 | 群聊中仅被 @提及 时才回复 |

## 工作原理

插件注册 channel key 为 `feishu_plus` 的 `FeishuPlusChannel`（独立 key，避免与内置 `feishu` 冲突而被 registry 跳过）。全部收发 / WebSocket / CardKit / 媒体能力继承自内置 `FeishuChannel`，覆写仅集中在话题逻辑上：

```
src/
├── plugin.json       # 插件清单（id: feishu-plus, type: channel）
├── plugin.py         # 入口：注册渠道 + 声明配置字段
├── channel.py        # FeishuPlusChannel —— 话题级 session 聚合、
│                     #   命令跳过引用、话题内流式卡片创建
└── cards_override.py # 话题感知的 tool-guard 审批卡片 render + handle
```

- **审批卡片**：复用上游 `tool_guard` 的无状态构造/解析函数，仅把「话题路由」收敛到本插件——render 时强制把 `feishu_thread_id` / `feishu_message_id` 写进按钮 `session_ctx`，handle 时读回并让 `/approval` 命令落回原话题。因此 site-packages 的 `tool_guard.py` / `context.py` 可保持上游版本。
- **话题内流式**：CardKit 流式 = ① `card.create` 得 `card_id` → ② 发 interactive 消息引用 `card_id` → ③ `card_element.content` 流式更新。仅 ② 受话题影响，插件让它在话题内改走话题内回复；若飞书话题不支持 CardKit 卡片，创建失败返回 `None`，自动回退为纯文本回复，不影响功能。
- **命令跳过引用**：父类会把被回复的 interactive 卡片正文前置成 `[quoted interactive: ...]`，使命令文本不再以 `/` 开头、前缀匹配失效；跳过获取意味着不发 Get Message 请求，引用内容对命令场景本就无意义。

## 已知限制

- 与内置「飞书」渠道互斥，启用本插件前需先停用内置渠道。

## License

[Apache-2.0](LICENSE) © boringcat
