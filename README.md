# QwenPaw Feishu Plus

QwenPaw 飞书渠道增强插件（channel plugin）。继承内置 `FeishuChannel`，只覆写话题（thread）相关逻辑，为群聊话题场景补齐三项能力：

- **话题内审批卡片** —— tool-guard 工具审批卡片发送到话题内（而非会话根），按钮点击后 `/approval` 命令也回注到原话题；
- **话题内流式输出** —— CardKit 流式卡片在话题内创建与更新，失败自动回退纯文本；以 `/` 开头的命令消息不创建流式卡片，回复保持纯文本；
- **命令消息跳过引用获取** —— 以 `/` 开头的消息不抓取被回复卡片的内容，避免 `[quoted interactive: ...]` 前缀导致命令匹配失效；
- **interactive 卡片 Markdown 渲染** —— 收到（含被回复引用）的 interactive 卡片渲染为结构化 Markdown（标题、正文、表格、@人名），修复内置实现丢失 `div` 正文、压成单行的问题；
- **正则触发规则** —— YAML 文件配置正则列表，群消息正文命中即免 @提及 触发回复，规则可携带场景上下文追加到正文末尾，也可指定群 chat_id 白名单限定生效群；
- **触发自动进话题** —— 正则触发且消息不在话题中时，机器人回复自动进入以该消息为根的话题；
- **触发规则管理命令** —— `/feishu-plus show-triggers` 查看生效规则、`/feishu-plus reload-triggers` 重新加载规则 YAML（加载失败保留上一份生效规则），经 `SlashCommandRegistry` 分派并定位当前渠道实例。

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
| `trigger_yaml_path` | 文本 | — | `<workspace>/feishu_plus_triggers.yaml` | 触发规则 YAML 路径，相对路径相对 workspace 解析 |
| `auto_thread_on_trigger` | 开关 | — | 关闭 | 正则触发且消息不在话题中时，回复自动进入以该消息为根的话题 |

### 触发规则 YAML

`trigger_yaml_path` 指向的文件（默认 `<workspace>/feishu_plus_triggers.yaml`）为 `triggers:` 列表，每条规则：

```yaml
triggers:
  - pattern: "^告警|P[012]"          # 正则（re.search 语义）
    context: "（运维告警场景，请按值班口径回复）"   # 可选：命中后追加到正文末尾
    chat_ids:                        # 可选：群 chat_id 白名单，缺省全部群生效
      - "oc_xxxxxxx"                 # 可多个，仅在这些群触发
  - pattern: "^小助手"                # 纯触发，不加上下文
```

- 群聊 **text** 消息正文命中任一 `pattern` 即触发回复，无需 @提及（`require_mention` 开启时同样生效；`require_mention` 关闭时所有消息本就触发，正则仍用于匹配 `context` 与自动进话题）；
- 命中的规则取列表中**第一条**匹配项；`context` 作为一行追加到发给 AI 的消息正文末尾（与用户 @触发 时正文直接可见一致，正文原样保留、不剥离关键词）；规则带 `chat_ids` 时仅当消息所在群在其中才参与匹配，正文命中但群不符会跳过该规则继续找下一条；
- 无效正则条目跳过并记日志，不影响其余条目；文件格式由 pydantic 模型校验（`triggers` 为列表、`pattern` 必填非空字符串、`context` 可选字符串、`chat_ids` 可选字符串列表且条目非空，多余字段 / 类型错误整份置空并记日志）；文件缺失 / 解析失败 / 结构不符只置空规则，不阻断渠道启动；
- 「触发自动进话题」开启时，命中消息若不在话题中，机器人回复经 `reply_in_thread` 进入以该消息为根的话题，话题内后续消息按 `thread_id` 共享同一会话上下文；@提及 触发的消息保持普通回复、不自动建话题。

### 触发规则管理命令

插件注册 `/feishu-plus` slash 命令（群聊中先 @提及 机器人再输入）管理当前渠道的触发规则：

| 命令 | 说明 |
| :--- | :--- |
| `/feishu-plus show-triggers` | 查看当前生效的触发规则：规则文件路径、`auto_thread_on_trigger` 开关、逐条 `pattern` / `context` / `chat_ids` 白名单；规则为空时给出原因（文件缺失 / 加载失败 / 文件中无规则） |
| `/feishu-plus reload-triggers` | 重新加载规则 YAML（改文件后免重启生效）。加载失败（文件缺失 / 非法 YAML / 结构错误）时**保留上一份生效规则**并返回原因，避免一次配置错误清空线上规则 |

无参数或未知子命令时返回用法说明。命令经 QwenPaw `SlashCommandRegistry` 分派（与内置 `/daemon` 等命令同一机制），handler 从会话的 `channel_manager` 定位当前「飞书+」渠道实例；渠道未启用时提示不可用。

## 工作原理

插件注册 channel key 为 `feishu_plus` 的 `FeishuPlusChannel`（独立 key，避免与内置 `feishu` 冲突而被 registry 跳过）。全部收发 / WebSocket / CardKit / 媒体能力继承自内置 `FeishuChannel`，覆写仅集中在话题逻辑上：

```
src/
├── plugin.json       # 插件清单（id: feishu-plus, type: channel）
├── plugin.py         # 入口：注册渠道 + 声明配置字段
├── channel.py        # FeishuPlusChannel —— 话题级 session 聚合、
│                     #   命令跳过引用、引用卡片 Markdown 渲染、
│                     #   话题内流式卡片创建、正则触发规则、自动进话题
├── card_markdown.py  # interactive 卡片 → Markdown 渲染器（纯逻辑）
└── cards_override.py # 话题感知的 tool-guard 审批卡片 render + handle
```

- **审批卡片**：复用上游 `tool_guard` 的无状态构造/解析函数，仅把「话题路由」收敛到本插件——render 时强制把 `feishu_thread_id` / `feishu_message_id` 写进按钮 `session_ctx`，handle 时读回并让 `/approval` 命令落回原话题。因此 site-packages 的 `tool_guard.py` / `context.py` 可保持上游版本。
- **话题内流式**：CardKit 流式 = ① `card.create` 得 `card_id` → ② 发 interactive 消息引用 `card_id` → ③ `card_element.content` 流式更新。仅 ② 受话题影响，插件让它在话题内改走话题内回复；若飞书话题不支持 CardKit 卡片，创建失败返回 `None`，自动回退为纯文本回复，不影响功能。
- **命令跳过引用**：父类会把被回复的 interactive 卡片正文前置成 `[quoted interactive: ...]`，使命令文本不再以 `/` 开头、前缀匹配失效；跳过获取意味着不发 Get Message 请求，引用内容对命令场景本就无意义。
- **命令消息不流式**：以 `/` 开头的命令消息在 `_before_consume_process` 预创建流式卡片时即被判定（`request` 正文首段以 `/` 开头），跳过预创建并把标志写到 request；`on_streaming_start` 读到标志直接返回、不建卡片，结果经父类 `on_streaming_end` 的纯文本回退发出。会话根与话题内两条路径均生效。
- **卡片 Markdown 渲染**：内置 `extract_interactive_text` 把卡片压成单行，且 CardKit v2 卡片 `div` 的正文在 `text.content` 键（不在其递归的 child keys 里）会整体丢失。插件改用自研渲染器（`card_markdown.py`）：标题 → `# 标题`、`div`/`markdown` 正文保留（`<text_tag>` 去壳、`<at id=..>` → @名字、`<img>` → [图片]，emoji 短代码保留原样）、原生 `table` → GFM 表格、`note` → `> ` 引用块、`hr`/`button` 忽略。v1 卡片（顶层 `elements` + `markdown` tag）与 v2 / CardKit（`body.elements`）均支持。被引用（回复）卡片以 `> ` Markdown 引用块前置到消息文本，直接收到的卡片消息同样渲染完整 Markdown；渲染失败自动回退父类行为。`@` 名字解析复用 `_get_user_name_by_open_id`（带缓存），应用无 contact 权限或解析失败时回退 `@open_id后4位`。
- **正则触发规则**：`_on_message` wrapper 在父类处理前完成匹配 —— 命中时改写事件数据（`context` 追加到 `content` 的 text 末尾、必要时注入 `thread_id = message_id`），并以 `ContextVar` 传递命中状态给 `_check_group_mention` 覆写以绕过 @提及 检查。规则带 `chat_ids` 时按消息 `chat_id` 白名单过滤：群不符的规则跳过、不参与命中。父类流程对改写无感知：引用块仍前置正文、slash 命令前缀判断不受末尾追加影响。自动进话题复用父类话题管道（session 按 thread 聚合、`_reply_in_thread` 回复、流式卡片进话题），飞书话题根消息的 `thread_id` 即其自身 `message_id`，后续话题内消息携带相同值，会话天然连续。

## 已知限制

- 与内置「飞书」渠道互斥，启用本插件前需先停用内置渠道。

## License

[Apache-2.0](LICENSE) © boringcat
