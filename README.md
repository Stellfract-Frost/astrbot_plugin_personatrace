# PersonaTrace 记忆 · AstrBot 插件

给 AstrBot 加一层**会过期的记忆**。不是"再挂几个工具"，而是每轮自动读写：

- **每轮读**：请求前把 2–3 条最该被想起的记忆塞进当前轮，不依赖模型愿不愿意调工具。
- **每轮写**：回复后丢线程池，用宿主自己的模型做抽取，节流后写入。

所以第一句话就可能"她记得你"，不用等冷启动。

后端是 [PersonaTrace](https://github.com/Stellfract-Frost/PersonaTrace)：每条记忆带起始时间、保质期，以及支撑它的原话。

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
