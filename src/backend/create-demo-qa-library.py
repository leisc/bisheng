"""建一个演示用 QA 知识库（type=1），并绑到「IT 运维助手」。

背景
----
文档知识库（type=0）与 QA 知识库（type=1）在 Bisheng 里是**同一张 knowledge 表、
type 不同的两类行**，但底层完全不同：

  * 文档库：文件 -> 解析 -> SplitterTransformer 切片(1000/100) -> 每片 embedding
    -> Milvus + ES。回答时召回的是**原文片段**，带 page/bbox，能定位出处。
  * QA 库：一条记录 = `questions: list[str]` + `answers: str`，**只对 question 做
    embedding，答案塞进 Milvus/ES metadata 的 extra.answer 字段**
    （`bisheng/api/services/knowledge_imp.py:455` QA_save_knowledge）。
    没有切片器、没有 chunk_size、没有 page/bbox。回答时召回的是**整条问答对**。

所以 QA 库适合一问一答边界清晰的场景：客服标准话术、IT 运维 FAQ、SOP 速查。
一条「主问题 + N 个相似问」会生成 N+1 个向量文档，共享同一个 file_id（QA 行 id），
这正是提高召回的手段 —— 本脚本手写相似问，不依赖 `qa_similar_model_id` 配置
（没配会报 10920「Not configuredQA Knowledge Base Similarity Question Model」）。

为什么绑 IT 运维助手
-------------------
`IT 运维助手` 原本只绑了知识空间 id=3（INSOFT 产品知识库）。assistant_agent.py
按 type 分流：space_ids -> build_space_knowledge_tool，其余 -> init_knowledge_tool，
所以 SPACE 与 QA 可以共存于同一个助手。加上 FAQ 后，产品问题走知识空间、
一线报障问题走 FAQ，一个助手覆盖两类问法，15 分钟 demo 少切一次页面。

注意 `PUT /assistant` 的 `knowledge_list` 是**全量覆盖**（AssistantUpdateReq，
bisheng/api/v1/schemas.py:294），所以必须把原有的 id=3 一起发回去，否则会静默
解绑知识空间。`temperature`/`max_token` 同理必须显式给（列是 NOT NULL，不传会 500）。

用法：
    .venv/bin/python create-demo-qa-library.py            # 执行
    .venv/bin/python create-demo-qa-library.py --check    # 只查不改
"""
from __future__ import annotations

import argparse
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))

EMBEDDING_MODEL_ID = "4"  # qwen3.7-text-embedding-flash
QA_LIBRARY_NAME = "INSOFT IT 运维 FAQ 库"
QA_LIBRARY_DESC = (
    "IT 服务台高频报障的标准处理口径：账号、网络、终端、会议、权限五类，"
    "面向全体员工，一问一答，命中即给可执行步骤。"
)
IT_ASSISTANT_NAME = "IT 运维助手"

# ─────────────────────────── 演示 QA 内容 ───────────────────────────
# q      主问题（embedding 的第一条，也是列表页显示的问题）
# similar 手写相似问，全部一起 embedding，提高口语化问法的召回
# a      答案。Milvus/ES 里不存答案正文，只存 extra.answer，所以这里写长一点
#        不影响检索，只在命中后作为上下文喂给 LLM。
QA_PAIRS = [
    {
        "q": "VPN 连不上，一直卡在验证身份那一步怎么办？",
        "similar": ["VPN 无法连接", "VPN 登录不上", "远程拨号失败", "VPN 验证身份卡住"],
        "a": (
            "按以下顺序排查：\n"
            "1. 确认账号密码正确，连续输错 5 次会锁定 30 分钟；\n"
            "2. 确认当前网络能访问公网（VPN 需要能出去）；\n"
            "3. 重启客户端，右下角图标右键「重新连接」；\n"
            "4. Windows 用户尝试「以管理员身份运行」打开客户端。\n"
            "以上无效，请带上报错截图和客户端版本号联系 IT 服务台：内线 8000，"
            "或 itsm@insoft.com，工作时间 9:00-18:00。"
        ),
    },
    {
        "q": "忘记域账号密码了，怎么重置？",
        "similar": ["密码忘了", "AD 密码过期", "域密码怎么改", "登录提示密码错误"],
        "a": (
            "三种方式：\n"
            "1. 自助重置：登录页点「忘记密码」，用绑定的手机号收验证码，"
            "5 分钟内可重设一次；\n"
            "2. 已登录其他终端：Ctrl+Alt+Del -> 更改密码；\n"
            "3. 以上都不行：带工牌到 IT 服务台（3 楼 308）或内线 8000 人工重置，"
            "需要本人确认，不接受邮件代办。\n"
            "注意：新密码需满足复杂度（10 位以上，含大小写+数字+符号），90 天强制更换。"
        ),
    },
    {
        "q": "Outlook 收不到邮件，别人说发了但我这边没有？",
        "similar": ["邮件收不到", "Outlook 不同步", "邮箱延迟", "邮件进垃圾箱了吗"],
        "a": (
            "排查步骤：\n"
            "1. 让发件人确认地址无误，并查他的「已发送」是否真的投递成功；\n"
            "2. 检查 Outlook 左下角「已删除邮件」和「垃圾邮件」文件夹；\n"
            "3. Web 端（mail.insoft.com）登录看一下，能收到说明是客户端同步问题；\n"
            "4. 客户端问题：文件 -> 账户设置 -> 选账户 -> 修复，或删掉本地 OST 缓存重建。\n"
            "若 Web 端也没有，大概率被邮件网关规则拦了，请联系 IT 服务台提供发件人"
            "地址和时间，网关侧可查投递日志。"
        ),
    },
    {
        "q": "共享盘提示没有权限，怎么申请？",
        "similar": ["访问共享文件夹被拒", "\\\\fs01 打不开", "没有权限访问", "部门盘权限申请"],
        "a": (
            "共享盘权限按部门-岗位自动发放，默认能看到本部门盘。需要额外权限时：\n"
            "1. 登录 IT 服务门户 -> 权限申请 -> 文件共享；\n"
            "2. 填写完整 UNC 路径（如 \\\\fs01\\finance\\2026）、用途、需要的时间段；\n"
            "3. 部门负责人审批，一般 1 个工作日内生效。\n"
            "临时支援（1-3 天）走「临时权限」通道，直属主管一句话即可批。\n"
            "提示：路径要复制资源管理器地址栏的原文，手写容易漏反斜杠。"
        ),
    },
    {
        "q": "新同事入职，账号和电脑什么时候能好？",
        "similar": ["新人入职 IT 准备什么", "入职账号开通", "新员工电脑申请", "onboarding IT 流程"],
        "a": (
            "标准节奏：\n"
            "* T-3 天：HR 在系统提交入职单，IT 收到工单自动建域账号、邮箱、"
            "企业微信；\n"
            "* T-1 天：按岗位配电脑（开发岗默认 32G 内存 + 双屏），预装基础环境；\n"
            "* T 日上午：到服务台领机，30 分钟内完成激活、加域、装必备软件；\n"
            "* 特殊需求（GPU 工作站、Mac、Linux）请在入职单备注，需提前 5 个工作日。\n"
            "入职当天本人带身份证原件，账号密码当面交付，不接受代领。"
        ),
    },
    {
        "q": "收到可疑邮件说我密码要过期让点链接，是钓鱼吗？",
        "similar": ["钓鱼邮件怎么判断", "邮件里的链接能点吗", "疑似诈骗邮件", "邮箱安全"],
        "a": (
            "基本可以确定是钓鱼。IT 部门**永远不会**用邮件让你点链接改密码。\n"
            "正确做法：\n"
            "1. 不要点链接、不要填任何信息、不要下载附件；\n"
            "2. Outlook 里点「举报 -> 网络钓鱼」，会同步给安全团队并全公司投递拦截；\n"
            "3. 已经点了并填了密码：立刻内线 8000，我们会在网关侧踢掉你的会话，"
            "并强制全端改密；\n"
            "4. 判断技巧：看发件人真实地址（不只看显示名）、悬停看链接真实目标、"
            "措辞制造紧迫感的基本都是。"
        ),
    },
    {
        "q": "会议室投屏没画面，线也换了还是不行？",
        "similar": ["投屏不显示", "HDMI 没信号", "会议室投影黑屏", "无线投屏连不上"],
        "a": (
            "按顺序试：\n"
            "1. 确认笔记本切到了正确输出源：Win+P 选「复制」或「扩展」，"
            "Mac 在系统设置 -> 显示器里选；\n"
            "2. 有些会议室要按面板上的「投屏」信号源键，不是自动切换；\n"
            "3. 无线投屏用 RoomCast，确认连的是房间号对应的热点（贴在桌上），"
            "密码每天变；\n"
            "4. 都无效大概率是矩阵或面板故障，报修时请提供会议室编号"
            "（门牌贴纸上的 M-xxx）。\n"
            "紧急会议可到 3 楼 308 备用会议室，有备份设备。"
        ),
    },
    {
        "q": "笔记本外接显示器没信号，识别不到第二屏？",
        "similar": ["外接屏不亮", "双屏设置", "扩展坞没反应", "HDMI 线新的也不行"],
        "a": (
            "1. 先确认线材和显示器本身好的（拿同事能用的线交叉测试）；\n"
            "2. 笔记本需要按 Fn + 投屏键（F4 或带显示器图标的键）切换输出；\n"
            "3. 用扩展坞的话，扩展坞固件和驱动要装，服务台可远程推；\n"
            "4. 显示设置里点「检测」，识别到了但没画面 = 分辨率/刷新率超限，"
            "降到 1080p60 再试；\n"
            "5. Type-C 转 HDMI 部分转接头不兼容 4K60，换 4K30 或直连 Type-C 线。\n"
            "硬件损坏（接口松动、屏幕排线）走资产更换，需主管确认。"
        ),
    },
    {
        "q": "怎么申请装一个软件？比如 Axure、Navicat？",
        "similar": ["软件安装申请", "能不能装 xx 软件", "正版软件去哪下", "软件商城"],
        "a": (
            "统一走软件商城（软件中心 -> 企业软件），能搜到的直接一键装，"
            "已买企业授权，不用自己找破解。\n"
            "商城里没有的：\n"
            "1. 在 IT 服务门户提交「软件采购/安装申请」，填用途、版本、"
            "是否商用、预算归属；\n"
            "2. 安全团队做合规审查（开源协议、数据出境、漏洞），2-5 个工作日；\n"
            "3. 涉及 license 费用的需部门负责人审批。\n"
            "注意：禁止私自安装来源不明的安装包，EDR 会拦并告警。"
        ),
    },
    {
        "q": "电脑突然很卡，风扇狂转，任务管理器看CPU很高？",
        "similar": ["电脑很卡怎么处理", "开机慢", "CPU 占用高", "电脑死机"],
        "a": (
            "1. Ctrl+Shift+Esc 打开任务管理器，按 CPU 排序，看是哪个进程；\n"
            "2. 常见元凶：浏览器标签开太多（建议不超过 30 个）、"
            "杀毒软件全盘扫描、索引服务、IDE 索引大项目；\n"
            "3. 结束不认识的进程前先搜一下名字，系统进程别乱杀；\n"
            "4. 重启能解决 80% 的临时性卡顿；\n"
            "5. 持续卡顿且机器超过 4 年，可申请换新（资产更换周期："
            "笔记本 4 年、台式 5 年）。\n"
            "如果某进程 CPU 长期 100%，请截图进程名报服务台。"
        ),
    },
    {
        "q": "WiFi 认证失败，一直弹登录页面进不去？",
        "similar": ["无线连不上", "wifi 认证页面", "portal 登录失败", "搜不到公司 wifi"],
        "a": (
            "公司无线是 INSOFT-Staff（员工）和 INSOFT-Guest（访客）。\n"
            "1. 连上后应自动弹认证页，没弹就浏览器打开 any.insoft.com；\n"
            "2. 用域账号登录，注意域名选 INSOFT，不是邮箱前缀；\n"
            "3. 提示「已在线数达上限」说明你的账号在别处挂着，"
            "旧设备忘记网络即可；\n"
            "4. 证书报错：安卓需手动信任 ca.insoft.com 的根证书"
            "（IT 服务门户 -> 下载 -> 证书）。\n"
            "5. 访客网络每天 0 点重置，仅限 8 小时。"
        ),
    },
    {
        "q": "服务器磁盘使用率告警了，先做什么？",
        "similar": ["磁盘满了怎么办", "磁盘告警处理", "/data 分区 90%", "日志把盘写满"],
        "a": (
            "应急三步：\n"
            "1. `df -h` 看是哪个分区，`du -sh /* | sort -rh | head` 定位大目录；\n"
            "2. 九成是日志和临时文件：\n"
            "   - 应用日志：/var/log/ 下超 30 天的 .log 可清（先确认没有在写）；\n"
            "   - 容器：`docker system prune -af`（**确认没有要保留的悬空卷**）；\n"
            "   - core dump、/tmp 下大文件；\n"
            "3. 清完仍 >80%，走扩容流程，不要在根分区硬塞。\n"
            "红线：不要直接 `rm -rf /var/log/*`，先 `lsof` 确认没有进程持有，"
            "否则文件删了空间不释放。生产环境操作需在变更单内执行并通知备份。"
        ),
    },
    {
        "q": "离职交接，账号和设备什么时候回收？",
        "similar": ["离职 IT 流程", "账号什么时候停", "离职电脑要交吗", "last day 注意事项"],
        "a": (
            "时间线：\n"
            "* 离职单生效日 T 日 18:00，域账号、邮箱、企业微信、"
            "VPN 全部自动停用（HR 提交离职单即排定，无需单独申请）；\n"
            "* T 日当天归还：笔记本、显示器、键鼠、门禁卡、加密狗。"
            "资产清单在入职时签过，漏还按资产原值赔付；\n"
            "* 个人盘（OneDrive 类）数据保留 30 天，之后清空，"
            "重要资料请提前转到部门盘；\n"
            "* 浏览器保存的密码、本地 git 凭据会随之失效，"
            "私人用途的账号记得提前解绑手机号。\n"
            "返聘或转正需重新走入职单，历史账号不恢复。"
        ),
    },
    {
        "q": "IT 服务台联系方式和响应时效？",
        "similar": ["怎么找 IT", "服务台电话", "报障多久能处理", "IT 值班时间"],
        "a": (
            "联系方式：\n"
            "* 内线 8000（工作时间 9:00-18:00，30 秒内接起）\n"
            "* itsm@insoft.com（留单，5 分钟内确认）\n"
            "* IT 服务门户 -> 提交工单（推荐，可跟踪进度）\n"
            "* 3 楼 308 现场服务台（领机、换新、紧急）\n\n"
            "时效承诺：\n"
            "* P1 生产故障 / 安全事件：15 分钟响应，2 小时给出方案；\n"
            "* P2 影响单人无法办公：2 小时响应，当日解决；\n"
            "* P3 一般咨询与申请：1 个工作日。\n\n"
            "非工作时间只有 P1 走值班手机（企业微信搜「IT 值班」），"
            "非紧急请留单，不要打值班电话。"
        ),
    },
]


# ─────────────────────────── 工具 ───────────────────────────


def load_setup_module():
    """复用 setup-demo-data.py 的 Client（含 F114 管理范围头与登录逻辑）。"""
    spec = importlib.util.spec_from_file_location(
        "setup_demo_data", os.path.join(HERE, "setup-demo-data.py")
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def data_of(r: dict):
    """兼容三种响应包装：data / data.data(list) / data(list)。"""
    d = r.get("data")
    if isinstance(d, dict):
        inner = d.get("data")
        if isinstance(inner, list):
            return inner
        return d
    return d


def list_of(r: dict) -> list:
    d = data_of(r)
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in ("list", "data", "items", "records"):
            if isinstance(d.get(k), list):
                return d[k]
    return []


def find_qa_library(cli) -> dict | None:
    r = cli.get("/knowledge", params={"page": 1, "page_size": 100, "type": 1})
    for k in list_of(r):
        if k.get("name") == QA_LIBRARY_NAME:
            return k
    return None


def existing_questions(cli, kid: int) -> set[str]:
    """把库里已有的问题全部收回来，用于幂等跳过。"""
    seen: set[str] = set()
    page = 1
    while True:
        r = cli.get(f"/knowledge/qa/list/{kid}", params={"page": page, "page_size": 100})
        if not cli.ok(r):
            return seen
        d = data_of(r)
        rows = d if isinstance(d, list) else (d.get("list") if isinstance(d, dict) else None)
        if not rows:
            return seen
        for row in rows:
            q = row.get("questions")
            if isinstance(q, list):
                seen.update(str(x) for x in q)
            elif q:
                seen.add(str(q))
        if len(rows) < 100:
            return seen
        page += 1


def it_assistant(cli) -> dict | None:
    r = cli.get("/assistant", params={"page": 1, "limit": 100})
    for a in list_of(r):
        if a.get("name") == IT_ASSISTANT_NAME:
            return a
    return None


# ─────────────────────────── 各阶段 ───────────────────────────


def phase_library(cli) -> int | None:
    print(f"\n━━━ QA 知识库（{QA_LIBRARY_NAME}）━━━")
    ex = find_qa_library(cli)
    if ex:
        kid = ex["id"]
        print(f"  = 已存在 (id={kid}, state={ex.get('state')})")
        return kid

    r = cli.post("/knowledge/create", json={
        "name": QA_LIBRARY_NAME,
        "description": QA_LIBRARY_DESC,
        "type": 1,
        "model": EMBEDDING_MODEL_ID,
    })
    d = data_of(r)
    if not (cli.ok(r) and d and d.get("id")):
        print(f"  ✗ 创建失败: {r.get('status_message')} {str(r)[:200]}")
        return None
    kid = d["id"]
    print(f"  + 已创建 (id={kid}, index={d.get('index_name')})")
    return kid


def phase_pairs(cli, kid: int) -> tuple[int, int]:
    print("\n━━━ 录入 QA 对 ━━━")
    seen = existing_questions(cli, kid)
    if seen:
        print(f"  库里已有 {len(seen)} 个问题，按主问题幂等跳过")
    added = skipped = failed = 0
    for pair in QA_PAIRS:
        if pair["q"] in seen:
            skipped += 1
            print(f"  = {pair['q'][:34]}")
            continue
        r = cli.post("/knowledge/qa/add", json={
            "knowledge_id": kid,
            "questions": [pair["q"], *pair["similar"]],
            "answers": [pair["a"]],
            "source": 1,
        })
        if cli.ok(r):
            added += 1
            print(f"  + {pair['q'][:34]}  (+{1 + len(pair['similar'])} 问)")
        else:
            failed += 1
            print(f"  ✗ {pair['q'][:34]}: {r.get('status_message')} {str(r)[:120]}")
    print(f"  小计：新增 {added}，跳过 {skipped}，失败 {failed}")
    return added, failed


def phase_bind(cli, kid: int) -> None:
    print(f"\n━━━ 绑定到「{IT_ASSISTANT_NAME}」━━━")
    a = it_assistant(cli)
    if not a:
        print(f"  ✗ 找不到助手 {IT_ASSISTANT_NAME}（先跑 setup-demo-data.py --assistant）")
        return
    aid = a["id"]

    # knowledge_list 是全量覆盖，必须把原有绑定一起发回去
    info = data_of(cli.get(f"/assistant/info/{aid}")) or {}
    cur = info.get("knowledge_list") or []
    cur_ids = [k.get("id") for k in cur if isinstance(k, dict) and k.get("id")]
    if kid in cur_ids:
        print(f"  = 已绑定 (assistant={aid}, knowledge_list={cur_ids})")
        return
    merged = cur_ids + [kid]

    # check_update_permission_async（bisheng/api/services/assistant.py:673）对
    # ONLINE(2) 的助手直接抛 10403「Assistant is live and not editable」。
    # 所以必须 下线 -> PUT -> 再上线。平台前端编辑已上线助手也是这套流程。
    was_online = a.get("status") == 2
    if was_online:
        off = cli.post("/assistant/status", json={"id": aid, "status": 1})
        if not cli.ok(off):
            print(f"  ✗ 下线失败: {off.get('status_message')} {str(off)[:160]}")
            return
        print("  · 已下线以便修改")

    payload = {
        "id": aid,
        "name": IT_ASSISTANT_NAME,
        "desc": info.get("desc") or "",
        "prompt": info.get("prompt") or "",
        "guide_word": info.get("guide_word") or "",
        "guide_question": info.get("guide_question") or [],
        "logo": "",
        "knowledge_list": merged,
        "temperature": 0.3,
        "max_token": 32000,
    }
    r2 = cli.put("/assistant", json=payload)
    if not cli.ok(r2):
        print(f"  ✗ 绑定失败: {r2.get('status_message')} {str(r2)[:200]}")
        if was_online:
            cli.post("/assistant/status", json={"id": aid, "status": 2})
            print("  · 已恢复上线")
        return
    print(f"  + 已绑定 (assistant={aid}, knowledge_list={merged})")

    if was_online:
        on = cli.post("/assistant/status", json={"id": aid, "status": 2})
        print(f"  {'+' if cli.ok(on) else '✗'} 重新上线: "
              f"{'' if cli.ok(on) else on.get('status_message')}")


def phase_verify(cli, kid: int) -> None:
    print("\n━━━ 校验 ━━━")
    r = cli.get(f"/knowledge/qa/list/{kid}", params={"page": 1, "page_size": 100})
    d = data_of(r)
    rows = d if isinstance(d, list) else (d.get("list") if isinstance(d, dict) else [])
    rows = rows or []
    bad = [x for x in rows if x.get("status") != 1]
    print(f"  列表页 QA 行数：{len(rows)}，非启用状态：{len(bad)}")
    for x in bad[:5]:
        print(f"    ! id={x.get('id')} status={x.get('status')} "
              f"remark={(x.get('remark') or '')[:100]}")

    a = it_assistant(cli)
    if a:
        info = data_of(cli.get(f"/assistant/info/{a['id']}")) or {}
        names = [k.get("name") for k in (info.get("knowledge_list") or [])]
        print(f"  {IT_ASSISTANT_NAME} 已绑知识：{names}")
    print(f"  平台端 /filelib「QA 知识库」tab 数据源 GET /knowledge?type=1，"
          f"应出现 {QA_LIBRARY_NAME}")


# ─────────────────────────── main ───────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只查不改")
    args = ap.parse_args()

    mod = load_setup_module()
    cli = mod.Client()
    cli.login()
    print("✅ 已登录", mod.ADMIN)

    ex = find_qa_library(cli)
    kid = ex["id"] if ex else None
    if args.check:
        if kid is None:
            print(f"\n[--check] 不存在 {QA_LIBRARY_NAME}，未做任何修改")
            return
        seen = existing_questions(cli, kid)
        print(f"\n[--check] QA 库 id={kid}，已有 {len(seen)} 个问题，未做任何修改")
        return

    if kid is None:
        kid = phase_library(cli)
        if kid is None:
            return
    else:
        print(f"\n━━━ QA 知识库（{QA_LIBRARY_NAME}）━━━")
        print(f"  = 已存在 (id={kid}, state={ex.get('state')})")

    added, failed = phase_pairs(cli, kid)
    phase_bind(cli, kid)
    phase_verify(cli, kid)

    print("\n━━━ 完成 ━━━")
    print(f"  QA 库：{QA_LIBRARY_NAME} (id={kid})")
    print(f"  新增 {added} 条 QA，失败 {failed} 条")
    print("\n  演示入口：")
    print("    平台端 http://localhost:3001/filelib -> 「QA 知识库」tab")
    print("    用户端 http://localhost:4001/workspace -> 「IT 运维助手」")
    print("    试问：「VPN 连不上怎么办」「忘记密码怎么重置」")


if __name__ == "__main__":
    main()
