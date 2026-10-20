# 用得越久，它越懂你

`PersonaTrace 记忆` · AstrBot 插件

**第一天**

　　它记住你叫什么、在忙什么。
　　你说「我不用微信」，它就再没提过微信。

**一周后**

　　你随口说的「这周末考试」，周六早上它先问了一句。
　　你换了工作，它不再拿旧公司的作息跟你说话。

**三个月后**

　　你很久没提吉他，它没催，只在那天问了句：「还在练吗？」
　　你说过「最近不想聊这个」，它记着，绕开了。

---

## 具体会做这些

- **记住你**：名字、在忙什么、上次聊到哪、你讨厌什么
- **到点会提**：「这周末考试」「下周三面试」「月底前交」这类话会被记住，到那天先问一句
- **说过不行的，不再推荐**：说过不接受远程办公，以后不会拿远程岗来问你
- **你改主意，它跟着改**：说「不学钢琴了」，它不会同时相信两种说法
- **记得你几点上线**：连续 3 天以上有样本才会用，不猜你的作息
- **重启、换会话、换群都还在**：不用重新做自我介绍

不用教它「记住这个」。

## 为什么大多数 bot 做不到

漏在两个地方：

1. **没人写**。挂个记忆 MCP 工具，写不写由模型自己决定，它经常想不起来。
2. **没人读**。就算写进去了，也没有东西把它放进下一轮对话。

这个插件让两件事固定发生：每轮请求前放进去 2–3 条最该被想起的，每轮回复后把新信息写下来。
注入部分压在 240 token 以内，不会撑爆上下文。

底层是 [PersonaTrace](https://github.com/Stellfract-Frost/PersonaTrace)。

## 安装

1. 在 AstrBot 的 `data/plugins/` 下：

   ```bash
   git clone https://github.com/Stellfract-Frost/astrbot_plugin_personatrace
   ```

   （或在插件市场直接安装）

2. 装核心包（尚未发布 PyPI，从仓库装）：

   ```bash
   pip install "personatrace @ git+https://github.com/Stellfract-Frost/PersonaTrace"
   ```

3. WebUI 里重载插件。启动日志会打印 `backend = sqlite (<路径>)`。

## 三句话验证它真的在工作

```
你：我在准备四级，这周末要模拟考
bot：……
你：/memory aiocqhttp:<你的QQ号>
bot：aiocqhttp:xxx：1 条
     - 在准备四级（还剩 2 天）
```

看到条数，就不是幻觉。

## 命令（仅管理员）

| 命令 | 作用 |
|---|---|
| `/memory <key>` | 查看某个人记住了什么（不传 key 查自己） |
| `/del <key>` | 彻底删除某个人的记忆，回执写明删了几条 |

`key` 形如 `aiocqhttp:1001`（平台:QQ 号）。删除不可逆——`invalidate` 才是留痕的"纠正"。

> 管理员命令要求 AstrBot 配置里的 `admins_id` 含你的 QQ 号；若设置了唤醒前缀，命令要带上。

## 配置

`_conf_schema.json` 里的项都能在 WebUI 改：

| 项 | 默认 | 说明 |
|---|---|---|
| `enabled` | true | 总开关 |
| `inject_max_items` | 3 | 每轮最多注入几条 |
| `inject_max_chars` | 240 | 注入文本上限，越小越省 token |
| `scope` | `per_user` | `per_group` 则整群共享一份关系记忆 |
| `window_cold_sec` / `window_warm_sec` | 120 / 600 | 抽取间隔（画像未成型 / 成型后） |
| `max_per_turn` | 8 | 单轮最多写入几条 |

## 适配

- 平台：**aiocqhttp（OneBot v11 反向 WebSocket）**，SnowLuma / NapCat 这类实现均可。平台名固定 `aiocqhttp`。
- 身份键 `aiocqhttp:<QQ号>`：私聊与群聊里同一个人共用一份；`per_group` 时用 `aiocqhttp:group:<群号>`。
- 不依赖 FastAPI、也不依赖 MCP。

## 已知限制

- 后端默认 SQLite（插件数据目录），重启不丢；装了 `redis` 且可用时优先用 Redis。
- 不带向量去重：靠 `uuid5(user:track:category:label)` 主键 + 命中强化，字面不同但同义的结论仍会分两条。
- `persona` 轨的部分类别（`traits` / `style_notes` / `interpersonal` / `boundaries`）没有专门的稳定度，会落到 60 天默认值。
- 抽取用宿主当前的模型，模型太弱时标签质量会下降。

## 开发

```bash
python -m pytest test_contract.py -q     # 11 项契约测试，不需要装 AstrBot
```

契约测试用假 `astrbot` 模块跑，覆盖：冷启动不注入、注入预算、同轮去重、双用户隔离、
命令、非法 key、写入腿、同步→异步桥，以及宿主的真实加载方式。

## License

MIT © 2026 Wang Zhiyuan
