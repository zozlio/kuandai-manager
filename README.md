# kuandai-manager

宽带账号台账 + 充值提醒 + AI 助手，一个 Docker 容器搞定。

给宽带代办点/运维人员用：把每个电话号码绑定的宽带账号、注册码、注册人、注册时间、
套餐费和剩余金额集中记下来，系统**每天按套餐自动扣一点余额**、余额不够自动生成催缴提醒，
还能直接用大白话问 AI。

---

## 功能

| 模块 | 说明 |
|---|---|
| **登录注册** | 首页自带登录/注册。第一次用的人自己设账户名和密码，**账户名和密码格式都不限制**（中文、空格、符号都行），只要不是空的 |
| **台账** | 电话号码、宽带账号、宽带注册码、注册人、**手机卡使用人**、注册时间、套餐原价、剩余金额、累计充值、运营商、备注 |
| **开号送 500** | 新建账号自动带 ¥500 初始余额，并记一笔「开号初始金额」流水 |
| **充值** | 列表页每行一个「充值」按钮，弹窗输入金额，确认后余额和累计充值同时更新，全程有确认/取消弹窗 |
| **七折开关** | 套餐原价 ¥399 打七折 = ¥279.30；开关一旦打开就锁死，不能再关（后端强制） |
| **按天扣费** | 月费 ÷ 当月天数 = 每日扣款额，每天自动从余额里扣一笔，月末补齐余数，整月刚好扣满一个月费 |
| **充值提醒** | 列表页实时打标（正常 / 余额偏少 / 余额不足），另有独立的催缴提醒页 |
| **收支流水** | 充值、扣费、手动调整全部留痕，余额变动可追溯 |
| **AI 问答** | 「哪些账号需要催缴？」「这个月一共要交多少钱？」直接问，AI 基于你的真实台账回答 |
| **AI 催缴话术** | 一键给待催缴账号生成一条 80 字内的催缴短信 |
| **页面设置** | 侧边栏「设置」页填接口地址、密钥、模型名，保存即生效，**不用重启容器** |

数据存在容器的 `/data` 目录（一个 SQLite 文件），挂载到宿主机即可备份和迁移。

---

## 飞牛 fnOS NAS 部署（本项目在 fnOS 上实测部署成功）

> 下面的 IP、端口、目录按你自己的 NAS 改。示例用的是常见的 fnOS 约定路径。

| 项目 | 值 |
|---|---|
| NAS 地址 | `192.168.1.10`（换成你自己的） |
| 访问地址 | <http://192.168.1.10:18080> |
| 项目目录 | `/vol1/1000/docker/kuandai-manager` |
| 数据目录 | `/vol1/1000/docker/kuandai-data`（数据库 `kuandai.db`） |
| 端口 | `18080`（**8080 已被飞牛网页端占用**，所以避开） |
| 镜像 | `kuandai-manager:latest` |

用的是 **Compose 项目**方式部署，所以在飞牛 Docker 面板的**「项目」标签页**里能看到
`kuandai-manager`，可以像别的项目一样点启停、看日志、改配置。

### 在面板里重建镜像（改完代码后）

项目目录里已经放好了 Dockerfile 和源码。在 NAS 上：

```bash
cd /vol1/1000/docker/kuandai-manager
docker build -t kuandai-manager:latest .
cd kuandai-manager && docker compose -f ../docker-compose.yml up -d
```

> **为什么 Dockerfile 默认走国内镜像站**
> 这台 NAS 上 BuildKit 直接拉 `docker.io` 会被劫持返回 HTML 页面（报
> `encountered unknown type text/html`），而 `docker.m.daocloud.io` 能正常访问。
> 所以 Dockerfile 的默认值已经写成国内镜像站 + 阿里云 pip/apt 源，**零参数直接构建即可**。
> 要用官方源，构建时覆盖：
> ```bash
> docker build \
>   --build-arg PYTHON_IMAGE=python:3.13-slim \
>   --build-arg DEBIAN_MIRROR= \
>   --build-arg PIP_INDEX_URL=https://pypi.org/simple/ \
>   --build-arg PIP_TRUSTED_HOST=pypi.org \
>   -t kuandai-manager:latest .
> ```

### 备份

```bash
# 备份数据库（推荐）
docker exec kuandai-manager tar -czf - -C /data kuandai.db > ~/kuandai-$(date +%F).db.tgz
# 或者直接复制宿主机文件
cp /vol1/1000/docker/kuandai-data/kuandai.db ~/backup/
```

---

## 快速开始

### 1. 构建镜像

```bash
cd kuandai-manager
docker build -t kuandai-manager:latest .
```

### 2. 启动

```bash
docker run -d \
  --name kuandai-manager \
  --restart unless-stopped \
  -p 8080:8080 \
  -v /opt/kuandai-manager/data:/data \
  -e AI_BASE_URL="https://api.minimaxi.com/v1" \
  -e AI_API_KEY="sk-你的key" \
  -e AI_MODEL="MiniMax-M3" \
  kuandai-manager:latest
```

浏览器打开 <http://服务器IP:8080> 即可使用。

> **第一次打开**：首页会让你创建管理员账户 —— 填一个账户名和密码就行，
> **不限制格式**（中文、带空格、带符号都能用），只要不是空的。
> 创建完就自动登录进台账。之后侧边栏底部可以随时退出。

> AI 相关的三个环境变量不填也能正常用，只是 AI 问答和话术生成会提示「未配置」——
> 这时候打开网页左侧的「设置」页，填上接口地址、密钥、模型名，保存就立刻生效，
> 不用再改 `-e`、也不用重建容器。
> 没填任何 AI 配置时，系统默认按 **MiniMax 中国版**（`https://api.minimaxi.com/v1` + `MiniMax-M3`）准备，
> 你只需要在设置页补上密钥就能用。

---

## 在页面上配置 AI（推荐）

打开网页 → 左侧「设置」→ 填三项：

| 填什么 | 例子 | 说明 |
|---|---|---|
| 接口地址 | `https://api.deepseek.com/v1` | 填到域名就行，系统自动补 `/v1` |
| 密钥 | `sk-xxxxxxxx` | 存下来了，页面上只显示尾巴 4 个字符，不会明文回显 |
| 模型名 | `deepseek-chat` | 你账号下能用的那个 |

三个按钮：

- **保存配置** —— 存下来，立刻生效（有确认弹窗）
- **测试连接** —— 真发一条消息验证地址/密钥/模型对不对，不改任何配置
- **恢复环境变量** —— 清掉页面上的配置，回落到容器启动时的环境变量

配置存在同一个 SQLite 文件里，跟着 `/data` 一起备份迁移。
页面上的配置**优先于**环境变量；某一栏留空，这一项就回落到环境变量。

---

## 环境变量

### AI 接口（OpenAI 兼容协议，换服务商只需改 base_url）

| 变量 | 必填 | 说明 |
|---|---|---|
| `AI_BASE_URL` | 否 | 接口地址，建议带 `/v1`。填了不带会自动补上。页面设置优先于此项 |
| `AI_API_KEY` | 否 | 密钥。页面设置优先于此项 |
| `AI_MODEL` | 否 | 模型名，填你账号下可用的那个。页面设置优先于此项 |

常见服务商示例：

| 服务商 | `AI_BASE_URL` | `AI_MODEL` 示例 |
|---|---|---|
| MiniMax 中国版（默认） | `https://api.minimaxi.com/v1` | `MiniMax-M3` |
| MiniMax 国际版 | `https://api.minimax.io/v1` | 以控制台可用模型为准 |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问（DashScope 兼容模式） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| 本地 Ollama | `http://host.docker.internal:11434/v1` | 你的本地模型名 |

> ⚠️ 国内密钥必须配国内域名（`api.minimaxi.com`），国际密钥配 `api.minimax.io`，
> 混用会报 `invalid api key`。老的 `api.minimax.chat` 域名已废弃，不要再用。

### 存储与调度

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATA_DIR` | `/data` | 数据库存放目录 |
| `DATABASE_URL` | 空 | 自定义数据库地址，默认 `sqlite:////data/kuandai.db` |
| `ENABLE_SCHEDULER` | `true` | 关掉定时任务（设为 `false`） |
| `DAILY_CHECK_HOUR` | `0` | 每日扣费巡检的小时（0-23），默认凌晨 0 点 |
| `DAILY_CHECK_MINUTE` | `5` | 每日扣费巡检的分钟 |
| `AI_TIMEOUT` | `120` | AI 接口超时秒数 |
| `AI_TEMPERATURE` | `0.3` | AI 回复随机性，催缴话术生成时会单独调高 |
| `SESSION_COOKIE_SECURE` | `false` | 改成 `true` 表示登录 cookie 只走 HTTPS。**正式对外访问时建议开**，但前面得有 Nginx 配好 HTTPS，否则会登不上 |

---

## 目录结构

```
kuandai-manager/
├── Dockerfile
├── requirements.txt
├── app/
│   ├── main.py          # 路由与页面
│   ├── auth.py          # 账户、密码哈希、登录会话
│   ├── models.py        # 数据表：台账 / 流水 / 提醒 / 用户 / 配置
│   ├── services.py      # 扣费、充值、提醒、统计逻辑
│   ├── ai.py            # AI 问答与话术生成
│   ├── scheduler.py     # 每日自动扣费
│   ├── database.py      # SQLite 连接与字段迁移
│   ├── config.py        # 环境变量配置
│   └── templates/       # 页面模板
├── static/style.css
└── scripts/entrypoint.sh
```

---

## 使用流程

1. **开号** —— 列表页「新增账号」，填手机号、宽带账号、注册码、注册人、手机卡使用人、注册时间、套餐原价
   （默认 399），保存后系统自动带 ¥500 初始余额，并记一笔「开号初始金额」流水
2. **要优惠就开七折** —— 编辑页打开「套餐七折优惠」开关，实际月费立刻变成 ¥279.30，
   每天扣款额同步变成 ¥9.31。**开关一旦打开就锁死**，避免账目来回改
3. **每天自动扣** —— 每天凌晨自动巡检：月费 ÷ 当月天数 = 每日扣款，从余额里扣一笔并记流水；
   月末会补齐余数，保证整月加起来正好等于一个月费
4. **余额不够** —— 扣不动时不扣款（余额保留着），改生成「待缴费」提醒，催缴页能一键生成话术
5. **客户充值** —— 列表页每行点「充值」→ 弹窗填金额（可点 +100/+200/+500 快捷键）
   → 弹窗实时显示「充值后余额」→ 点「确认充值」，余额和累计充值一起更新，全程有确认/取消按钮
6. **查账** —— 账号详情页有「收支流水」，充值、扣费、手动调整全部留痕；AI 助手页直接问：
   「13800138000 这个账号什么情况」「哪些欠费最多」

> 所有会改数据的操作（保存、充值、删除）都会先弹确认框，点「取消」不会有任何改动。

---

## 备份

数据只有一个文件：`/data/kuandai.db`

```bash
# 备份
docker exec kuandai-manager tar -czf - -C /data kuandai.db > ~/kuandai-$(date +%F).db.tgz

# 或者直接复制宿主机上的文件
cp /opt/kuandai-manager/data/kuandai.db ~/backup/
```

---

## 上线建议

- 现在**自带登录验证**了，但密码是明文不设复杂度要求（用户要求不限制格式），所以：
- 放在内网或专线后面最稳妥
- 对外开放时，前面加一层 Nginx 做 HTTPS，并把 `SESSION_COOKIE_SECURE` 设成 `true`：
  ```nginx
  location / {
      proxy_pass http://127.0.0.1:8080;
      proxy_buffering off;   # 流式输出必须关缓冲
      proxy_read_timeout 600s;
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
  }
  ```
- 换模型不用重建镜像：直接在「设置」页改模型名保存即可（也可以改 `-e` 环境变量后重启容器）

---

## 常见问题

**Q：为什么余额每天少一点，而不是每月扣一次？**
A：套餐费按天分摊。每天扣 = 月费 ÷ 当月天数（11 月 30 天，399 ÷ 30 ≈ ¥13.30/天）。
这样你随时打开列表都能看到「这个月还剩多少钱」，不会等到月底才发现扣不动。

**Q：七折打开后还能关吗？**
A：不能。前端开关会变成灰色不可点，后端也会直接忽略关闭请求。这是故意的——
套餐价反复改会让历史流水对不上账。

**Q：改了月费或余额，但自动扣费没反应？**
A：自动扣费在每天凌晨执行（`DAILY_CHECK_HOUR`）。想立刻执行，列表页点「立即巡检」。

**Q：累计充值和余额有什么区别？**
A：余额是现在还剩多少；累计充值是这个账号从开号到现在一共充过多少钱（开号送的 500 也算）。
累计充值只增不减，用来对账。

**Q：客户已经充值了，但提醒还在？**
A：列表页点「充值」把余额补上，相关提醒会自动关闭。

**Q：AI 提示「未配置」？**
A：到左侧「设置」页填上接口地址、密钥、模型名，保存就生效；不用管环境变量。
（环境变量只在你没在页面上填的时候才生效，而且 `docker run` 的 `-e` 只在创建容器时生效。）

**Q：账户名和密码有什么格式要求？**
A：**没有要求**。中文、英文、数字、空格、特殊符号都行，只要不是空的。
账户名不区分大小写（`Admin` 和 `admin` 算同一个），密码原样保存、不做长度或复杂度限制。

**Q：第一个注册的人就是管理员吗？**
A：是。第一次打开首页会让你「创建管理员账户」，建出来的就是管理员。
之后首页还能继续注册新账户，**默认都是普通用户**，谁都能进来看台账。

**Q：密码忘了登不进去怎么办？**
A：目前没有「找回密码」功能（因为密码是加密存着的，系统也读不出来）。
最省事的办法是停掉容器，把数据目录里的 `kuandai.db` 里 `users` 表的对应记录处理掉，
再重新注册；或者直接删掉整个数据库文件重来（**会连台账数据一起没了，记得先备份**）。

**Q：怎么加新的定时任务时间？**
A：重启容器前改 `-e DAILY_CHECK_HOUR=3 -e DAILY_CHECK_MINUTE=0`。
