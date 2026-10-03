# 双人见证资料封存服务

本地账号、密钥操作的资料封存/开封服务。封存后正文不可修改、不可（在开封前）被读取；
开封必须由封存人发起请求（理由 + 截止时刻，可选**最早开封时刻**），并在截止前取得
**两名不同指定见证人**分别同意；同意必须绑定本次请求与封存时的摘要。
资料未开封时，封存人可在见证人无法履职后**替换指定见证人**，新见证人可继续为
进行中的开封请求补票；封存人也可就未开封资料向另一名本地账号**发起交接邀请**，
受让人在截止前确认后成为新封存人。封存人还可为资料**安排到期清除时刻**：
到点清空正文（保留摘要、参与方与审计元数据），到期前可改期或取消，
交接后由新封存人管理。当前封存人或当前指定见证人可在到点开封、清除等结算
完成后**签发审计快照**（资料编号、摘要、当前状态与开封/清除时刻、按原顺序
排列且标明截止序号的审计记录，不含正文或密钥），由服务端持久化的 Ed25519
密钥签名；离线复核人凭“快照 + 签名 + 事先信任的公钥”验真。当前封存人还可
把**已开封**资料的正文临时交给无账号的外部复核人：凭资料编号与带时区截止
时刻签发**一次性阅览凭证**（截止时刻须在未来且早于已安排的清除时刻；
凭证明文不可猜测、库内只存摘要、明文仅签发时返回，响应同时给出供撤销的
凭证编号），复核人凭明文在截止前读取一次正文与摘要，并发兑换至多一次成功，
封存人可凭凭证编号在兑换前撤销。账号持有人可凭当前 `X-API-Key` **轮换密钥**：
新密钥明文仅在轮换响应中返回一次、库内只存摘要，旧密钥自轮换提交后立即失效
（重启后仍然失效），轮换与封存/同意/交接/清除等已认证写并发时按事务提交先后
判定。资料开封、到期清除、见证人替换与交接确认
时，系统还会在状态变更的同一事务内向当时的封存人/见证人**投递站内通知**，
账号凭现有 `X-API-Key` 以事件序号游标分页读取本人通知、按通知编号标记已读。
封存人还可发起**需见证人确认的预封存**：提交正文、三名互异且非本人的已注册
见证人及带时区的未来确认截止时刻，取得提案编号与正文摘要（响应不含正文）；
仅发起人本人与受邀见证人可凭编号查看提案，见证人凭摘要各确认一次（摘要不符、
重复确认拒绝且不计票），三人在截止前确认后在同一事务内按现有规则生成一份
封存资料并返回资料编号，重复请求不再生成资料；发起人可在完成前撤回，截止时
未集齐即失效，撤回/失效/完成后立即清空提案正文。
技术栈：Python 3.11 + FastAPI + SQLite。

## 安全与业务规则

| 规则 | 实现方式 |
| --- | --- |
| 本地账号，凭密钥操作 | `POST /register` 返回一次性 API Key，请求头 `X-API-Key` 携带；服务端只存 SHA-256 哈希 |
| 密钥轮换 | `POST /me/api-key/rotate` 凭**当前**密钥换取新密钥：新明文为 256 位随机串、**仅本次响应返回一次**，库内只保存其 SHA-256 摘要（旧摘要被覆盖，不保留历史密钥），旧密钥自轮换提交后立即失效并随 WAL 持久化（重启后仍失效）；轮换本身追加一条 `api_key_rotated` 审计（不含密钥材料） |
| 轮换与业务写并发 | 所有已认证写操作在 `BEGIN IMMEDIATE` 锁内先复核密钥是否仍是当前摘要：**轮换先提交**则持旧密钥并发的封存/同意/撤销/替换/交接/清除安排/签发凭证/撤销凭证/标记已读/签发快照一律 `401` 并整体回滚——业务写入不生效、不留下业务审计，也不触发连带结算；**业务写先提交**则照常完成，轮换随后提交。无效或已失效密钥沿用既有 `401`，不区分账号是否存在；未轮换账号与无需登录的兑换/验真行为不变 |
| 账号停用 | `POST /me/deactivation` 凭**当前**密钥申请：停用临界区先按现有规则做一轮全局到点结算（到点开封→到期清除→开封请求到期→交接邀请逾期→预封存提案逾期）；结算后若本人仍是任一**未清除资料的封存人**、任一**未开封资料的当前见证人**、任一**待确认预封存的发起人/受邀见证人**或任一**待确认交接的受让人**，返回 `409` 与逐项义务（类型/对象编号/原因），**账号保持可用**，但**本次到点结算仍已落库**；义务全部结清则立即停用——`key_hash` 覆盖为不可匹配值并置 `status=deactivated`（追加一条 `account_deactivated` 审计），当前密钥**立即且永久失效**（后续访问沿用无效密钥 `401`，不区分账号是否存在），**同名不可重新注册**（沿用注册 `409`）；历史审计、通知与参与记录全部保留，不删除任何行 |
| 停用后不可再被指定 | 停用账号不能再被其他账号指定为直接封存/预封存的见证人、见证人替换的新人选或交接的受让人：各指定路径在写锁内只接受 `status='active'` 的账号，停用账号一律按**现有无效参与人错误**处理（封存/预封存 `400`「见证人尚未注册」、替换 `400`「新见证人尚未注册」、交接 `400`「受让人尚未注册」，文案与未注册一致，不暴露停用状态） |
| 停用并发裁定 | 停用与封存、替换、交接、预封存、密钥轮换并发时全部按 `BEGIN IMMEDIATE` 写事务**提交顺序**裁定：停用先提交则并发角色指定在锁内复查到已停用账号而按上述既有错误回滚（不新增角色），并发业务写持有的旧密钥复核失败按既有 `401` 回滚；业务写先提交则新增角色可能构成义务，停用随后被 `409` 拒绝；不存在“停用后新增角色”或部分停用 |
| 封存需三名不同见证人 | 封存时校验三人互异、均已注册、且不含封存人本人 |
| 预封存提案 | 封存人可发起需见证人确认的预封存：提交正文、三名互异且非本人的已注册见证人及带时区未来确认截止时刻；响应只给提案编号与 SHA-256 摘要，**不含正文**。仅发起人本人与三名受邀见证人可凭编号查看摘要/截止时刻/状态/确认人数，其他账号（含凭编号试探、不存在编号）统一 `403` 不区分是否存在 |
| 预封存确认 | 三名受邀见证人凭提案摘要在截止前各确认一次：摘要不符、重复确认一律 `409` 拒绝且不计票（`UNIQUE(proposal_id, witness_id)` 兜底）；非受邀见证人/发起人本人确认统一 `403` |
| 三人齐生成资料 | 第三名不同见证人确认的**同一写事务内**按“现有封存规则”生成一份普通封存资料（documents + 见证名单 + `seal` 审计，与直接封存完全一致）并返回 `document_id`；提案置 `confirmed` 并清空正文。已完成提案的任何重复确认/重复请求一律 `409`，**不再生成第二份资料** |
| 预封存撤回/到期 | 发起人可在完成前撤回（`revoked`）；截止时刻未集齐三人即失效（`expired`，后台扫描器、每个提案写事务与提案查询都做确定性到期判定，停机期间到期重启恢复扫描补结算）；撤回、失效、完成后提案正文立即从库中清空，迟到确认/撤回一律 `409`；失败与不确认不产生资料或封存审计。发起/确认/撤回的截止判定以**取得写锁后的实际时刻**为准：等待写锁期间越过截止的，发起 `400` 不落提案，确认/撤回先失效清正文再 `409`，不计票、不生成资料 |
| 保存正文及摘要 | 正文原文与 SHA-256 摘要同表保存；封存后无任何更新接口，内容不可改 |
| 开封前不泄露正文 | 封存/待开封状态下查询/列表/错误响应均不含 `body`；非参与方一律 `403`，不区分资料是否存在 |
| 开封请求 | 封存人提交 `reason` 与带时区的 `deadline`（必须晚于当前时间） |
| 最早开封时刻（可选） | `earliest_open_at`：带时区，不早于提交时刻且严格早于 `deadline`；不填时行为不变（两票集齐即开封） |
| 两票同意 | 截止前两名**不同**见证人各同意一次才开封；`UNIQUE(request_id, witness_id)` 保证同一人重试只计一次 |
| 同意绑定摘要 | 同意时须提交封存摘要，服务端比对一致才记录，防止对被篡改内容背书 |
| 定时开封 | 填了 `earliest_open_at` 时，两票在该时刻前集齐只进入 `waiting`（待开封）；到点自动开封。到点时不足两票的，截止前补齐第二票时立即开封 |
| 到点必开封 | 到点前未撤销且已集齐两票的请求，自到点起只能开封：后台扫描器与每个写事务都先做确定性到点判定，即使后台处理延迟，之后的撤销或到期扫描也不能使其失效 |
| 未开封可替换见证人 | 封存人在资料未开封时可把一名指定见证人换为新人：新人须已注册、非封存人、不在当前名单，替换后仍为三名不同见证人；旧见证人随即失去访问与同意权限 |
| 替换不破坏进行中请求 | 被替换者对本次请求的同意立即失效、其他同意保留；有效票不足两票时请求由 `waiting` 退回 `pending`，新见证人凭封存摘要补票；已到最早开封时刻且两票已齐则先开封并拒绝替换；过截止时刻的请求按原规则失效后再完成替换 |
| 撤销/到期作废 | 到点**之前**封存人可撤销；过截止时刻始终不足两票则置为 `revoked`/`expired`，其同意永久失效；新请求从 0 票开始 |
| 未开封可交接封存人 | 封存人可就未开封资料向另一已注册本地账号发起交接邀请（受让人不能是本人或当前见证人），填写带时区确认截止时刻；每份资料同时至多一份待确认且未过期邀请；封存人可撤回；受让人须在截止前凭本人密钥确认 |
| 交接确认结算顺序 | 确认临界区先做确定性到点开封与开封请求到期判定：资料若已开封则拒绝交接；否则进行中的开封请求置 `invalidated`（过截止时刻的按原规则置 `expired`）并删除全部旧同意，再转移封存人身份；见证名单、正文、摘要不变 |
| 交接后权限 | 原封存人立即失去该资料的查看/操作权限（统一 403，不区分资料是否存在）；新封存人可重新申请开封，新请求从 0 票开始；开封前任何接口与错误体都不含正文 |
| 邀请生命周期 | `pending` 邀请可撤回（`revoked`）或逾期（`expired`，后台扫描与每个交接写事务都会确定性判定）；已撤回/逾期/资料开封后均不可确认；撤回或逾期后封存人可重新邀请；资料开封会连带使待确认邀请失效 |
| 到期清除正文 | 封存人凭密钥提交带时区的未来清除时刻（`PUT /documents/{id}/purge`），到期前可改期（再次 `PUT`）或取消（`DELETE`）；只有当前封存人可操作，交接后清除安排保留并改由新封存人管理 |
| 到期必清除 | 到点无论扫描器是否准时运行，后续查询与操作都呈现已清除结果：后台扫描器、每个写事务与资料查询都先做确定性到点清除判定——清空正文（库中 `body` 置空）、状态置 `purged`、保留摘要/参与方/审计元数据、任何接口不再返回正文；`purged_at` 记为约定的清除时刻 |
| 清除的连带结算 | 已开封资料仍按期清除；未开封资料清除时：进行中开封请求置 `invalidated`（过截止时刻的按原规则 `expired`）并删除全部旧同意、待确认交接邀请置 `expired` |
| 清除与开封的时序 | 清除与最早开封同刻时**清除优先**（不开封，请求失效）；最早开封时刻严格更早时**先开封**，到清除时刻再清正文；延迟处理也按时刻比较得出同一历史 |
| 已清除不可再操作 | 已清除资料不能再开封、替换见证人、发起/确认交接或再安排清除；已到期的改期或取消一律 `409`；一切拒绝路径不泄露正文 |
| 拒绝且状态不变 | 失效凭据(401)、非见证人(403)、无效时限(400/422)、重复开封(409) 等任何拒绝路径都不写数据（到点自动开封/清除的提交除外：状态变更先落库，拒绝在提交后返回） |
| 并发只产生一个结果 | 全部“检查 + 写入”在 SQLite `BEGIN IMMEDIATE` 短事务内完成；并发同意/撤销/定时开封/到期/替换见证人/交接发起、撤回、确认/到期清除只有一个已提交终态（同意在锁内以“当前名单”复核见证人身份；交接与第二票同意赛跑时要么开封要么交接，互斥且唯一） |
| 操作留痕、重启一致 | 每次成功操作按自增序号追加 `audit_log`（含 `handover_invited/confirmed/revoked/expired`、`request_invalidated`、`purge_scheduled/rescheduled/canceled`、`purged`、`audit_snapshot_issued`）；WAL 持久化，重启时先跑一轮恢复扫描，状态、票数、审计顺序完全一致 |
| 审计快照签发 | 当前封存人或当前指定见证人可签发：快照含资料编号、摘要、当前状态与开封/清除时刻、按原顺序排列且标明截止序号（`audit_upto`）的审计记录，不含正文或密钥；签发在同一写事务内先完成到点开封/到期清除等确定性结算再取数，快照对应一个已提交的一致状态 |
| 快照签名与离线验真 | 服务端 Ed25519 密钥持久化于 `signing_keys` 表（重启后旧签名仍可验证）；复核人提交快照、签名及事先信任的公钥验真，字段或记录顺序被篡改、签名不匹配、版本不支持或公钥无效均明确失败（400），不以包内自带公钥建立信任 |
| 一次性阅览凭证签发 | 当前封存人为**已开封**资料凭资料编号与带时区阅览截止时刻签发：截止时刻须晚于当前且**严格早于已安排的清除时刻**（未安排清除不能签发）；明文为 256 位不可猜测随机串，库内只存 SHA-256 摘要，明文仅签发响应返回一次，响应同时给出供撤销使用的凭证编号 `pass_id` |
| 凭证一次性兑换 | 外部复核人**无账号、无需任何凭据**，凭明文在截止前可且仅可读取一次正文及摘要；条件更新（`WHERE status='active'`）+ 写事务行锁原子翻转状态，并发兑换**至多一次成功**，成功后凭证立即失效 |
| 凭证撤销 | 当前封存人凭凭证编号可在**兑换前**撤销（非封存人/不存在统一 403，已兑付/已撤销/已过期/资料已清除 409）；撤销后兑换统一 404 |
| 兑换与清除同事务 | 兑换（及撤销）临界区先在同一事务结算到期清除；**清除时刻与兑换同刻以清除优先**——清除先提交落库、兑换统一拒绝且不返回正文；兑换不因结算清除而留下兑付记录 |
| 凭证失败不泄露 | 无效凭证、过期、撤销、已兑付、已清除资料的兑换**统一 404 同一文案**，无法区分凭证/资料是否存在，绝不返回正文；任何失败兑换不写审计 |
| 凭证审计 | 签发、成功兑换、撤销均追加到原有 `audit_log` 审计链（动作 `view_pass_issued`/`view_pass_redeemed`/`view_pass_revoked`；外部复核人兑换的 `actor` 为 `null`）；凭证相关事件同样进入审计快照 |
| 站内通知投递 | 资料**开封**（`opened`）或**到期清除**（`purged`）时，在状态变更的同一写事务内向“当时封存人 + 三名见证人”各记一条；**见证人替换**（`witness_replaced`）时通知封存人和新旧见证人；**交接确认**（`handover_confirmed`）时通知新旧封存人及三名见证人。收件人按状态变更时刻的名单在事务内固定 |
| 通知事件序号 | 通知的 `event_seq` 固定为该状态变更**主审计记录的 `audit_log.id`**（全局事件序号），通知按 `(event_seq, id)` 升序排列；同一事件对每名收件人恰好一条，`UNIQUE(recipient_id, event_seq)` 与“同事务写入”共同保证扫描器与请求并发结算同一变化不重复投递 |
| 通知最小内容 | 通知只含资料编号 `document_id`、事件类型 `event_type`、事件时刻 `at`、已读状态 `is_read`（外加通知编号与事件序号），**不含正文、凭证或密钥**；标记已读不写审计链 |
| 通知读取与已读 | 账号凭现有 `X-API-Key` 读取本人通知：`GET /notifications?cursor=<非负事件序号>&page_size=<1～100>`，返回本人通知与下一游标（`next_cursor`，无更多时为 `null`）；`POST /notifications/{id}/read` 按通知编号标记已读并返回已读状态 |
| 通知参数与越权拒绝 | 非法游标（负数/非整数）或页大小（不在 1～100）一律 `422` 且不改状态；标记他人或不存在的通知编号统一 `403`（不区分是否存在）且不改状态；已读重试幂等（保留首次 `read_at`，不产生额外记录）；无凭据 `401` |
| 失权不影响收件箱 | 被替换的旧见证人、交接后的旧封存人等失去资料权限的人，仍可读取/标记自己收到的通知；但通知不含正文/凭证/密钥，不能借此访问资料（资料接口仍按参与方规则 `403`） |
| 失败不留通知 | 通知与状态变更在同一个 `BEGIN IMMEDIATE` 事务内写入：失败或回滚的操作（错误同意、无效替换、被拒交接、撤销、邀请逾期等）不留通知；仅成功的开封/清除/替换/交接确认才投递 |

### 请求状态机

```
pending  ──(两票集齐，未设最早开封时刻 或 已到最早开封时刻)──▶ opened
pending  ──(两票集齐但未到 earliest_open_at)───────────────▶ waiting
waiting  ──(到达 earliest_open_at；扫描器或任意写事务触发)──▶ opened（不可再撤销/过期）
waiting  ──(封存人替换投票见证人，有效票不足两票)───────────▶ pending（新见证人补票）
pending  ──(封存人在截止前撤销)─────────────────────────────▶ revoked
pending  ──(过 deadline 仍不足两票)─────────────────────────▶ expired
pending/waiting ──(受让人在交接邀请截止前确认交接)──────────▶ invalidated（旧同意删除）
pending/waiting ──(到达清除时刻)────────────────────────────▶ invalidated（旧同意删除）
```

> 替换见证人不改变资料的 `sealed` 状态，只改变当前见证人名单，并按上表调整
> 进行中请求的票数与状态；交接封存人也不改变资料状态，只把进行中的开封请求
> 置为 `invalidated`（过截止时刻的仍为 `expired`）并转移封存人；`sealed/opened`
> 是资料生命周期，`pending/waiting` 等是开封请求的状态，两者互不混淆。

### 资料生命周期

```
sealed ──(两名见证人同意，且已到最早开封时刻)──▶ opened
sealed ──(到达清除时刻 purge_at)───────────────▶ purged（正文清空，不再返回）
opened ──(到达清除时刻 purge_at)───────────────▶ purged（正文清空，不再返回）
```

> `purged` 是终态：正文从库中清空，摘要、参与方与审计元数据保留，
> 任何接口不再返回正文；已清除资料不能再开封、替换见证人、交接或再安排清除。
> 清除与最早开封同刻时清除优先（不开封）；最早开封严格更早时先 `opened`
> 再于清除时刻 `purged`。

后台扫描器（默认每秒一次）在一轮内先把到点的 `waiting` 请求确定性开封
（清除优先的不开），再把到清除时刻的资料清除正文，然后把已到截止时刻仍
`pending` 的请求置为 `expired`，最后把过确认截止时刻仍 `pending` 的交接
邀请置为 `expired`；所有涉时刻判定在写事务内（资料查询对清除判定同理）
再做一次，杜绝临界时间竞态。

## 一键启动（Docker Compose）

```bash
docker compose up --build -d
curl http://127.0.0.1:8000/health
```

镜像构建时安装依赖；容器入口 `entrypoint.sh` 会幂等初始化库表
（`/data/seal.db`，位于 Docker 卷 `seal-data`，重启数据不丢）。

停止：`docker compose down`（加 `-v` 同时删除数据卷）。

## 本地启动（不用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export SEAL_DB_PATH=./data/seal.db   # SQLite 文件路径（默认 /data/seal.db）
export PORT=8000                      # 监听端口（默认 8000）
export SWEEP_INTERVAL_SECONDS=1       # 到期扫描间隔（默认 1 秒）
python init_db.py                     # 可选：单独初始化库表（应用启动也会初始化）
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- 服务地址：`http://127.0.0.1:8000`
- 交互式文档：`http://127.0.0.1:8000/docs`
- 健康检查：`GET /health`

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SEAL_DB_PATH` | `/data/seal.db` | SQLite 数据库文件路径（自动创建，WAL 模式） |
| `PORT` | `8000` | 监听端口（容器入口使用） |
| `SWEEP_INTERVAL_SECONDS` | `1` | 到期请求后台扫描间隔秒数 |
| `HOST_PORT` | `8000` | 仅 Compose 使用：宿主机映射端口 |

## HTTP API

所有接口除 `/register`、`/health`、`/view-passes/redeem`、
`/audit-snapshots/public-key`、`/audit-snapshots/verify` 外均需请求头
`X-API-Key: <注册时返回的密钥>`。后三个面向无账号外部复核人，无需任何凭据。
时间字段使用带时区 ISO-8601，例如 `2026-10-01T12:00:00Z` 或 `2026-10-01T20:00:00+08:00`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/register` | 注册，返回一次性 `api_key` |
| GET | `/me` | 查询当前密钥对应的账号 |
| POST | `/me/api-key/rotate` | 凭当前 `X-API-Key` 轮换密钥：新明文 `api_key` 仅本次响应返回一次，旧密钥即刻失效 |
| POST | `/me/deactivation` | 凭当前 `X-API-Key` 申请停用账号：先按现有规则到点结算；有未了义务返回 `409` 与义务清单（账号保持可用，结算已落库）；无义务则立即停用，密钥永久失效、同名不可重注册 |
| POST | `/documents` | 封存资料：`{body, witnesses:[3人]}`，返回 `digest`（无正文） |
| POST | `/pre-seal-proposals` | 发起需见证人确认的预封存：`{body, witnesses:[3人], confirm_deadline}`，返回提案编号与摘要（无正文） |
| GET | `/pre-seal-proposals/{id}` | 查看提案（仅发起人/受邀见证人）：摘要、截止时刻、状态、确认人数、完成后的资料编号；不含正文 |
| POST | `/pre-seal-proposals/{id}/confirmations` | 受邀见证人凭摘要确认：`{digest}`；第三票同事务生成封存资料并返回 `document_id` |
| POST | `/pre-seal-proposals/{id}/revoke` | 发起人在完成前撤回预封存提案 |
| GET | `/documents` | 列出本人封存/见证的资料（仅元数据，含 `purge_at`/`purged_at`） |
| GET | `/documents/{id}` | 详情；开封后才含 `body`、`reason`、`witness_approvals`；已清除返回 `purged` 与清除时刻 |
| POST | `/documents/{id}/open-requests` | 封存人提交开封请求：`{reason, deadline, earliest_open_at?}` |
| POST | `/open-requests/{id}/approve` | 见证人同意：`{digest}`（须等于封存摘要）；第二票可能返回 `waiting` 或 `opened` |
| POST | `/open-requests/{id}/revoke` | 封存人撤销：`{reason?}` |
| POST | `/documents/{id}/witness-replacements` | 封存人替换一名指定见证人（资料未开封）：`{old_witness, new_witness}` |
| POST | `/documents/{id}/handover-invitations` | 封存人就未开封资料发起交接邀请：`{to_username, confirm_deadline}` |
| POST | `/handover-invitations/{id}/confirm` | 受让人在截止前凭本人密钥确认；返回新封存人与旧请求结果 |
| POST | `/handover-invitations/{id}/revoke` | 封存人撤回待确认邀请 |
| GET | `/handover-invitations/{id}` | 查询邀请状态（邀请双方或资料参与方可见，仅元数据） |
| PUT | `/documents/{id}/purge` | 封存人安排/改期到期清除时刻：`{purge_at}`（带时区、未来时刻） |
| DELETE | `/documents/{id}/purge` | 封存人在清除时刻到来前取消已安排的清除 |
| POST | `/documents/{id}/view-passes` | 封存人为已开封资料签发一次性阅览凭证：`{expires_at}`（带时区、未来、早于 `purge_at`）；明文 `token` 仅此一次返回 |
| POST | `/view-passes/redeem` | **无需凭据**：外部复核人凭 `{token}` 在截止前兑换一次正文与摘要 |
| POST | `/view-passes/{id}/revoke` | 当前封存人凭凭证编号在兑换前撤销 |
| GET | `/documents/{id}/audit` | 按顺序返回该资料的全部操作记录 |
| POST | `/documents/{id}/audit-snapshots` | 当前封存人或当前指定见证人签发审计快照，返回快照、签名与公钥标识 |
| GET | `/audit-snapshots/public-key` | 查询快照签名公钥与标识（无需凭据，供复核人事先建立信任） |
| POST | `/audit-snapshots/verify` | 离线验真：`{snapshot, signature, public_key}`，无需凭据 |
| GET | `/notifications` | 读取本人通知：查询参数 `cursor`（非负事件序号，默认 0）、`page_size`（1～100，默认 20）；返回 `notifications` 与 `next_cursor` |
| POST | `/notifications/{id}/read` | 把本人的一条通知标记已读，返回 `{notification_id, is_read, read_at}`；他人/不存在编号统一 `403` |

状态码：成功 `200/201`；参数校验失败 `422`（如时间不带时区、`earliest_open_at`
不早于 `deadline`）；业务校验失败 `400`（如三人不互异、截止时刻已过、最早开封时刻
早于当前、替换时新见证人未注册或为封存人、交接受让人未注册/为本人/为见证人、
确认截止时刻已过、清除时刻已过、阅览截止时刻不晚于当前）；未认证 `401`；无权/不可见 `403`
（含被替换掉的旧见证人再访问/再同意、交接后原封存人再访问/再管理清除、非受让人确认、
非封存人撤回/安排清除、非封存人签发/撤销阅览凭证、凭证编号不存在）；未找到 `404`（被替换者不在当前名单）；
冲突（重复同意、重复开封、请求已撤销/到期、waiting 下再同意、已有进行中请求、
新见证人已在名单、资料已开封仍替换、重复发起待确认交接邀请、确认已撤回/逾期/
已确认的邀请、交接时资料已开封、资料已清除仍开封/替换/交接/安排清除/签发阅览凭证、
清除时刻已到仍改期/取消、无清除安排仍取消、撤销已兑付/已撤销/已过期凭证等）`409`。
预封存接口：三名见证人不互异/含发起人本人、见证人未注册、确认截止时刻已过为
`400`；时间不带时区为 `422`；非参与方查看/确认、发起人本人确认、非受邀见证人
确认、非发起人撤回、凭不存在编号访问统一 `403`（不区分提案是否存在）；摘要不符、
同一见证人重复确认、已完成提案再确认/撤回、已撤回/已失效提案再操作、到期后
迟到确认或撤回为 `409`；无/错凭据 `401`。
凭证兑换的全部失败（无效明文、过期、撤销、已兑付、资料已清除）统一
**`404` 同一文案**，不区分原因、不泄露正文或资料存在性。
通知接口：游标或页大小非法为 `422`；标记他人或不存在的通知编号统一 `403`
（不改状态、不区分是否存在）；缺少/错误凭据 `401`。
密钥轮换：缺少凭据 `401` `{"detail":"缺少 API Key"}`；无效密钥或**已被轮换
作废的旧密钥**统一 `401` `{"detail":"凭据无效"}`（与注册/登录既有的 401 完全
一致，不区分账号是否存在）；轮换后持旧密钥发起任何已认证写操作同样得到该
`401` 且事务整体回滚（不生效、不留业务审计）。
账号停用：缺少/错误/已失效密钥均为既有 `401`；有未了义务为 `409`，错误体为
`{"detail":{"message":"...","obligations":[{"type","ref_kind","ref_id","reason"}]}}`
（`type` 取值 `document_owner`/`document_witness`/`pre_seal_owner`/
`pre_seal_witness`/`handover_assignee`）；停用成功为 `200`，此后同名注册沿用
`409`；把已停用账号指定为见证人/替换新人选/受让人沿用各接口既有无效参与人
`400`（文案同“尚未注册”）。
错误体统一为 `{"detail": "..."}`（停用被拒时 `detail` 为含义务清单的对象），
不含任何正文内容。

### 最早开封时刻（earliest_open_at）

- 字段可选，带时区 ISO-8601；必须**不早于提交时刻**且**严格早于** `deadline`。
- 不填写时一切照旧：两票集齐立即开封，响应里 `earliest_open_at` 为 `null`。
- 填写后，两票在该时刻前集齐只把请求置为 `waiting`（待开封）：同意响应与
  `GET /documents/{id}` 都会返回 `status: "waiting"` 与 `earliest_open_at`，
  但仍不返回 `body` 与见证明细。
- 到达 `earliest_open_at` 后由后台扫描器（或任意并发写事务，二者等价且互斥）
  自动开封；若到点时只有一票，则保持 `pending`，截止前第二票到达的**同一请求**
  立即开封。
- 到点前可以撤销；一旦到点且两票已齐，撤销与到期扫描都不再生效。

```bash
# 提交带最早开封时刻的请求：最早 1 小时后、截止 24 小时后
EARLIEST=$(python3 -c "import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=1)).isoformat())")
DEADLINE=$(python3 -c "import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=24)).isoformat())")
curl -sS -X POST $BASE/documents/1/open-requests \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d "{\"reason\":\"家庭会议\",\"deadline\":\"$DEADLINE\",\"earliest_open_at\":\"$EARLIEST\"}"
# -> {"request_id":1,"status":"pending",...,"earliest_open_at":"…"}

# 两票在时刻前集齐 -> waiting（而非 opened）
curl -sS -X POST $BASE/open-requests/1/approve -H "X-API-Key: $W1_KEY" \
  -H 'Content-Type: application/json' -d '{"digest":"ab12…"}'
# -> {"approval_count":1,"status":"pending",...}
curl -sS -X POST $BASE/open-requests/1/approve -H "X-API-Key: $W2_KEY" \
  -H 'Content-Type: application/json' -d '{"digest":"ab12…"}'
# -> {"approval_count":2,"status":"waiting","earliest_open_at":"…"}

# 待开封期间查询：有 earliest_open_at 与 waiting，仍无 body
curl -sS $BASE/documents/1 -H "X-API-Key: $OWNER_KEY"
# 到达最早开封时刻后（扫描间隔内）再查 -> status=opened，可见 body
```

### 替换指定见证人（witness replacement）

资料**未开封**时，封存人可提交被替换者与新见证人账号，把一名指定见证人换成新人。

- `POST /documents/{id}/witness-replacements`，请求体
  `{"old_witness": "<被替换者用户名>", "new_witness": "<新见证人用户名>"}`。
- 新见证人必须**已注册**、**不是封存人本人**、且**不在当前三名名单中**
  （含不能与被替换者相同）；替换后仍是三名互不相同的见证人。
- 只有**封存人本人**可调用；资料已开封（含替换事务内确定性到点开封）后再替换一律 `409`。
- **无进行中请求**（或上一轮已 `revoked/expired`）：直接更新名单，`open_request` 为 `null`。
- **有进行中的 `pending` 请求**：被替换者对本次请求的同意立即删除（失效），其他见证人
  的同意保留；若有效票因此不足两票（`pending` 下本就少于两票），新见证人可调用现有
  同意接口 `POST /open-requests/{id}/approve`，提交**封存摘要**为本次请求补票。
- **请求处于 `waiting`（两票已齐待到点）**：
  - 替换的是**投票者** → 有效票不足两票，请求**由 `waiting` 退回 `pending`**，
    保留另一票，新见证人补票后两票在最早时刻前再次进入 `waiting`；
  - 替换的是**未投票的第三名见证人** → 两票不受影响，维持 `waiting`。
- **到达最早开封时刻且两票已齐**：替换请求进入写临界区时请求**先开封**，
  替换随即 `409` 被拒（名单不变），即使后台扫描器尚未运行也一样。
- **已过截止时刻**：进行中的请求按原规则置 `expired`（旧票作废），替换随后照常完成，
  返回的 `open_request.status` 为 `expired`、`approval_count` 为 `0`。
- 替换成功后旧见证人**立即失去资料访问权与同意权**（查询/同意返回 `403`，
  名单/列表同步更新）；本次变更追加一条 `witness_replaced` 审计记录（必要时其前
  追加一条 `request_expired`）。

成功响应 `200`：

```json
{
  "document_id": 1,
  "status": "sealed",
  "witnesses": ["bob", "erin", "dave"],
  "open_request": {"request_id": 7, "status": "pending", "approval_count": 1}
}
```

`open_request` 为 `null` 表示当前无进行中请求；否则给出该请求的 `request_id`、
当前 `status`（`pending`/`waiting`/`expired`）与**有效票数** `approval_count`。
响应与其他未开封接口一样不含正文 `body`。

```bash
# 无进行中请求：直接换人（dave 无法履职 -> erin 接替）
curl -sS -X POST $BASE/documents/1/witness-replacements \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d '{"old_witness":"dave","new_witness":"erin"}'
# -> {"document_id":1,"status":"sealed","witnesses":["bob","carol","erin"],"open_request":null}

# 进行中请求里替换已投票的 carol：其同意失效，有效票清零，请求退回待同意
curl -sS -X POST $BASE/documents/1/witness-replacements \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d '{"old_witness":"carol","new_witness":"erin"}'
# -> {"open_request":{"request_id":7,"status":"pending","approval_count":0}, ...}

# 新见证人 erin 复用现有同意接口、凭封存摘要为本次请求补票
curl -sS -X POST $BASE/open-requests/7/approve -H "X-API-Key: $ERIN_KEY" \
  -H 'Content-Type: application/json' -d '{"digest":"ab12…"}'
# -> {"approval_count":1,"status":"pending"}

# 被替换的 carol 立即无权再同意
curl -sS -o /dev/null -w '%{http_code}\n' -X POST $BASE/open-requests/7/approve \
  -H "X-API-Key: $CAROL_KEY" -H 'Content-Type: application/json' -d '{"digest":"ab12…"}'
# -> 403

# 拒绝示例（均不改名单/票数，且不泄露正文）：
#   新见证人未注册 400；新见证人是封存人 400；新见证人已在名单 409；
#   被替换者不在当前名单 404；非封存人调用 403；资料已开封 409。
```

### 封存人交接（handover）

资料**未开封**时，封存人可向另一名本地账号发起交接邀请，受让人在确认截止前
凭本人密钥确认后成为新封存人。

- `POST /documents/{id}/handover-invitations`，请求体
  `{"to_username": "<受让人用户名>", "confirm_deadline": "<带时区 ISO-8601 时刻>"}`。
- 受让人必须**已注册**、**不是封存人本人**、且**不在当前见证人名单中**；
  `confirm_deadline` 必须带时区且晚于当前时间。邀请待确认期间，封存人也不能
  把受让人替换进见证人名单（`409`），保证确认后不会出现“新封存人兼见证人”。
- 每份资料同时**至多一份**待确认（`pending`）且未过期的邀请：重复发起 `409`；
  先撤回或等其逾期后可重新邀请。只有**封存人本人**可发起与撤回。
- 封存人可 `POST /handover-invitations/{id}/revoke` 撤回待确认邀请；
  受让人可 `POST /handover-invitations/{id}/confirm` 在截止前确认；
  `GET /handover-invitations/{id}` 查询状态（邀请双方或该资料当前参与方可见，仅元数据）。
- **已撤回、已逾期、已确认的邀请不能确认**；资料在邀请期间开封的，邀请连带
  失效（`expired`），同样不能确认。逾期判定既由后台扫描器执行，也在每个交接
  写事务内确定性再做一次，不依赖扫描及时性。
- 确认在同一写临界区内按顺序结算：
  1. 确定性处理到点开封与开封请求到期；
  2. 资料若**已开封**（含临界区内刚触发的到点开封）则拒绝交接（`409`），身份不变；
  3. 否则把进行中的开封请求置为 `invalidated`（若其已过截止时刻则按原规则
     `expired`），并**删除其全部旧同意**（旧同意永久失效，不能用于后续请求）；
  4. 转移封存人身份；**见证名单、正文、摘要均不变**。
- 交接成功响应（`200`）在邀请状态之外还返回：

  ```json
  {
    "invitation_id": 4,
    "status": "confirmed",
    "from_username": "alice",
    "to_username": "erin",
    "document_id": 2,
    "document_status": "sealed",
    "current_owner": "erin",
    "previous_owner": "alice",
    "witnesses": ["bob", "carol", "dave"],
    "old_open_request": {"request_id": 7, "status": "invalidated", "approval_count": 0}
  }
  ```

  `old_open_request` 为 `null` 表示无进行中开封请求；否则给出旧请求 `request_id`、
  终态（`invalidated`/`expired`）与清零后的票数。响应不含正文 `body`。
- 交接后**原封存人立即失去**该资料的查看/列表/审计与一切操作权限（统一 `403`，
  不区分资料是否存在）；**新封存人可重新申请开封**，从 0 票开始，由同三名
  见证人重新两票同意。见证人名单与权限不变。
- 成功操作返回邀请状态；每次成功发起/撤回/确认/逾期、旧请求失效与成功交接均
  追加审计（`handover_invited`、`handover_revoked`、`handover_confirmed`、
  `handover_invitation_expired`、`request_invalidated`、`request_expired`）。

```bash
# 发起交接邀请（确认截止时刻取 1 小时后，支持 +08:00 等带时区写法）
DEADLINE=$(python3 -c "import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=1)).isoformat())")
curl -sS -X POST $BASE/documents/1/handover-invitations \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d "{\"to_username\":\"erin\",\"confirm_deadline\":\"$DEADLINE\"}"
# -> {"invitation_id":1,"status":"pending","to_username":"erin",...}

# 受让人 erin 凭本人 X-API-Key 在截止前确认
curl -sS -X POST $BASE/handover-invitations/1/confirm -H "X-API-Key: $ERIN_KEY"
# -> {"status":"confirmed","current_owner":"erin","previous_owner":"alice",
#     "witnesses":["bob","carol","dave"],
#     "old_open_request":{"request_id":7,"status":"invalidated","approval_count":0}}

# 原封存人随即无权访问；新封存人可重新申请开封
curl -sS -o /dev/null -w '%{http_code}\n' $BASE/documents/1 -H "X-API-Key: $OWNER_KEY"
# -> 403
curl -sS -X POST $BASE/documents/1/open-requests \
  -H "X-API-Key: $ERIN_KEY" -H 'Content-Type: application/json' \
  -d "{\"reason\":\"新封存人申请\",\"deadline\":\"$DEADLINE\"}"
# -> 201，新请求 approval_count 从 0 开始

# 撤回待确认邀请（封存人）；撤回/逾期后均返回 409 不能确认，可重新邀请
curl -sS -X POST $BASE/handover-invitations/1/revoke -H "X-API-Key: $OWNER_KEY"
# -> {"invitation_id":1,"status":"revoked",...}

# 拒绝示例（均不改状态、不转移身份、不泄露正文）：
#   受让人未注册/为本人/为见证人 400；确认截止时刻已过 400；时间不带时区 422；
#   非封存人发起/撤回 403；非受让人确认 403；无/错密钥 401；
#   重复发起待确认邀请 409；确认已撤回/逾期/已确认邀请 409；资料已开封 409。
```

### 到期清除正文（scheduled purge）

封存人可为资料安排一个带时区的未来清除时刻：到点清空数据库业务记录中的正文，
资料状态置为 `purged`，保留摘要、参与方（封存人/见证人）与审计元数据，
此后任何接口不再返回正文。到期前可改期或取消；交接后清除安排保留，
由新封存人管理，原封存人无权操作。

- `PUT /documents/{id}/purge`，请求体 `{"purge_at": "<带时区 ISO-8601 时刻>"}`：
  未安排过时为**安排**，已安排过时为**改期**；`purge_at` 必须带时区且**晚于当前时间**
  （不带时区 `422`、已过 `400`）。已开封资料同样可安排（到点照常清除正文）。
- `DELETE /documents/{id}/purge`：**取消**已安排的清除；没有安排时 `409`。
- 只有**当前封存人**可安排/改期/取消（其他人 `403`，不区分资料是否存在）；
  交接确认后原封存人立即失去该权限。
- **到期必清除**：后台扫描器、每个写事务与资料查询都会先做确定性到点清除判定，
  即使扫描器延迟或停机（重启时恢复扫描也会补清），后续查询与操作都呈现已清除结果。
  响应中的 `purged_at` 记为约定的清除时刻，与处理及时性无关。
- **连带结算**（与交接同一写临界区语义）：未开封资料的进行中开封请求置
  `invalidated`（已过截止时刻的按原规则 `expired`）并删除全部旧同意；
  待确认交接邀请置 `expired`。
- **时序规则**：清除与最早开封同刻时**清除优先**（不开封，请求失效）；
  最早开封时刻严格更早时**先开封**，到清除时刻再清正文（审计依次为
  `opened`、`purged`）；即使处理延迟到两个时刻之后，也按时刻比较得出同一历史。
- **已清除资料**不能再提交开封请求、同意、替换见证人、发起/确认交接或再安排
  清除（均 `409`）；已到期的改期或取消同样 `409`；一切拒绝路径不泄露正文。
- 成功响应 `200`：

  ```json
  {
    "document_id": 1,
    "status": "sealed",
    "purge_at": "2026-10-02T00:00:00+00:00",
    "previous_purge_at": null
  }
  ```

  改期时 `previous_purge_at` 为原时刻；取消响应为
  `{"document_id": 1, "status": "sealed", "purge_at": null}`。
  已清除资料的详情返回 `status: "purged"`、`purge_at`、`purged_at`、`digest`
  与见证名单等元数据（曾开封过的还保留 `opened_at`、`witness_approvals`），
  不含 `body`。
- 审计动作：`purge_scheduled`（安排）、`purge_rescheduled`（改期）、
  `purge_canceled`（取消）、`purged`（清除生效），连带结算复用
  `request_invalidated` / `request_expired` / `handover_invitation_expired`。

```bash
# 安排 24 小时后清除（支持 +08:00 等带时区写法）
PURGE_AT=$(python3 -c "import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=24)).isoformat())")
curl -sS -X PUT $BASE/documents/1/purge \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d "{\"purge_at\":\"$PURGE_AT\"}"
# -> {"document_id":1,"status":"sealed","purge_at":"…","previous_purge_at":null}

# 改期到 48 小时后
PURGE_AT2=$(python3 -c "import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=48)).isoformat())")
curl -sS -X PUT $BASE/documents/1/purge \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d "{\"purge_at\":\"$PURGE_AT2\"}"
# -> {"document_id":1,"status":"sealed","purge_at":"…","previous_purge_at":"…"}

# 到期前取消
curl -sS -X DELETE $BASE/documents/1/purge -H "X-API-Key: $OWNER_KEY"
# -> {"document_id":1,"status":"sealed","purge_at":null}

# 到期后查询（扫描器或查询本身触发确定性清除）：
curl -sS $BASE/documents/1 -H "X-API-Key: $OWNER_KEY"
# -> {"document_id":1,"status":"purged","purge_at":"…","purged_at":"…",
#     "digest":"ab12…","witnesses":["bob","carol","dave"], ...}   # 无 body

# 拒绝示例（均不改状态、不泄露正文）：
#   非封存人安排/取消 403；无/错密钥 401；清除时刻已过 400；不带时区 422；
#   资料已清除仍安排/改期/取消 409；清除时刻已到仍改期/取消 409；
#   没有安排仍取消 409；已清除资料再开封/替换见证人/交接 409。
```

### 密钥轮换（API key rotation）

账号持有人怀疑密钥外泄或需要定期换钥时，可凭**当前仍有效**的 `X-API-Key`
自助轮换，无需联系其他参与方，也不影响任何资料、开封请求、交接邀请、清除
安排、凭证与通知：

- `POST /me/api-key/rotate`（需 `X-API-Key`，无请求体）。服务端在
  `BEGIN IMMEDIATE` 写事务内先复核当前密钥，再生成新密钥并覆盖摘要。
  成功 `200`：

  ```json
  {
    "user_id": 3,
    "username": "alice",
    "api_key": "9f1c…（64 位十六进制，256 位随机，仅本次响应返回一次）",
    "rotated_at": "2026-10-02T03:10:00.000000+00:00"
  }
  ```

- **只存摘要、一次性返回**：新密钥与注册密钥同为 `secrets.token_hex(32)`，
  数据库 `users.key_hash` 仅保存其 SHA-256（旧摘要被覆盖，不保留任何历史
  密钥）；明文只出现在轮换响应这一次，此后无法找回，丢失后只能再次用当前
  有效密钥轮换。轮换本身在审计链追加一条 `api_key_rotated` 记录（细节文本
  不含任何新旧密钥材料）。
- **旧密钥立即且持久失效**：事务一旦提交，旧密钥对全部已认证接口（读、写
  一致）返回与“随机无效密钥”完全相同的 `401`
  `{"detail":"凭据无效"}`；失效结论随 WAL 落盘，**重启后仍然失效**，不存在
  宽限期。新密钥即刻可用于全部既有接口，账号的资料归属、见证身份、通知
  收件箱等全部不变。
- **与业务写并发：按提交先后判定**。轮换与封存、见证同意/撤销、见证人替换、
  交接发起/撤回/确认、清除安排/改期/取消、阅览凭证签发/撤销、通知标记已读、
  审计快照签发等已认证写操作共用同一把写锁；每个业务写进入写临界区后第一件
  事就是在锁内重新比对 `users.key_hash`：
  - **轮换先提交**：持旧密钥的业务写复核失败，按既有 `401` 语义拒绝并整体
    回滚——既不会写入业务数据，也不会留下业务审计，更不会借事务前部的
    “到点开封/到期清除”结算产生连带写入（确定性结算由后台扫描器与后续合法
    请求照常完成）；
  - **业务写先提交**：该业务写在锁内复核时密钥仍有效，照常完成并落审计，
    随后提交的轮换同样成功，旧密钥自轮换提交起失效。
- **读路径同样复核**：资料查询、列表、审计、邀请状态等只读接口在需要触发
  “到点清除”确定性结算时，也在其写事务内复核密钥——旧密钥请求不得借读路径
  产生清除写入；未到清除时刻的纯只读不涉及任何写入，返回既有 `401` 即可。
- **不改变既有行为**：未发起过轮换的账号一切照旧；无需登录的
  `/health`、`/view-passes/redeem`、`/audit-snapshots/public-key`、
  `/audit-snapshots/verify` 不校验密钥，行为与响应完全不变；错误密钥、
  已失效旧密钥、不存在账号的密钥三者响应不可区分，不泄露账号存在性。

```bash
# 凭当前密钥轮换（响应里的 api_key 只显示这一次，请立即保存）
curl -sS -X POST $BASE/me/api-key/rotate -H "X-API-Key: $OLD_KEY"
# -> {"user_id":3,"username":"alice","api_key":"9f1c…（仅此一次）","rotated_at":"…"}

# 旧密钥即刻失效：读与写都得到与“无效密钥”相同的 401
curl -sS $BASE/me -H "X-API-Key: $OLD_KEY"
# -> {"detail":"凭据无效"}
curl -sS -o /dev/null -w '%{http_code}\n' -X POST $BASE/documents \
  -H "X-API-Key: $OLD_KEY" -H 'Content-Type: application/json' \
  -d '{"body":"…","witnesses":["bob","carol","dave"]}'
# -> 401（资料不会创建，也不写审计）

# 新密钥立即用于一切既有操作
curl -sS $BASE/me -H "X-API-Key: $NEW_KEY"
```

可运行的端到端示例：`python examples/demo_api_key_rotation.py`
（覆盖单次/再次轮换、旧密钥读写 401、无效密钥同文案、未轮换账号与无凭据
接口不受影响）。

### 账号停用（account deactivation）

账号持有人可凭**当前** `X-API-Key` 申请停用本人账号（无请求体）。服务端在
同一个 `BEGIN IMMEDIATE` 写事务内：

1. **先按现有规则全局到点结算**：到点开封 → 到期清除 → 开封请求到期 →
   交接邀请逾期 → 预封存提案逾期（与后台扫描器同一顺序、同一结算函数）；
2. 以结算后的当前状态核对**未了义务**：
   - 任一**未清除**（`sealed`/`opened`）资料的**封存人**；
   - 任一**未开封**（`sealed`）资料的**当前见证人**；
   - 任一**待确认**（`pending`）预封存提案的**发起人/受邀见证人**；
   - 任一**待确认**（`pending`）交接邀请的**受让人**；
3. 有未了义务：`409` 拒绝，逐项返回义务的类型、对象种类/编号与原因，
   **账号保持可用**；即使被拒，第 1 步的到点结算也**随事务提交落库**；
4. 无未了义务：立即停用——密钥永久失效、同名不可重注册，历史
   审计/通知/参与记录全部保留。

停用成功后：当前密钥的一切后续访问沿用现有无效密钥 `401`
（`{"detail":"凭据无效"}`，与随机假密钥完全一致，不区分账号是否存在）；
同名重新注册沿用注册既有 `409`（`{"detail":"用户名已存在"}`）。其他账号
把停用账号指定为见证人/替换新人选/交接受让人时，按各接口**现有无效参与人
错误**处理（`400`，文案与“尚未注册”一致，不暴露停用状态）。

```bash
# 有未了义务（例：本人是未清除资料的封存人）-> 409 + 义务清单
curl -sS -X POST $BASE/me/deactivation -H "X-API-Key: $OWNER_KEY"
# -> 409
# {"detail":{"message":"仍有未了义务，账号停用被拒绝；账号保持可用",
#   "obligations":[
#     {"type":"document_owner","ref_kind":"document","ref_id":1,
#      "reason":"本人是未清除资料的封存人（资料未开封），须先完成交接或到点清除"}]}}
# 义务类型：document_owner / document_witness /
#          pre_seal_owner / pre_seal_witness / handover_assignee
# 此刻账号仍可用：GET /me -> 200；义务解除（交接/到点清除/替换/提案终结）后可重试

# 无未了义务 -> 200，立即停用
curl -sS -X POST $BASE/me/deactivation -H "X-API-Key: $OWNER_KEY"
# -> {"user_id":3,"username":"alice","status":"deactivated","deactivated_at":"…"}

# 停用后：当前密钥立即且永久失效（与无效密钥同一 401）
curl -sS $BASE/me -H "X-API-Key: $OWNER_KEY"
# -> 401 {"detail":"凭据无效"}

# 同名不可重新注册
curl -sS -X POST $BASE/register -H 'Content-Type: application/json' \
  -d '{"username":"alice"}'
# -> 409 {"detail":"用户名已存在"}

# 其他账号把停用账号指定为见证人/受让人 -> 现有无效参与人 400（不暴露停用）
#   封存/预封存：400 {"detail":"见证人尚未注册: alice"}
#   替换新见证人：400 {"detail":"新见证人尚未注册"}
#   交接受让人：  400 {"detail":"受让人尚未注册"}
```

可运行的端到端示例：`python examples/demo_account_deactivation.py`
（覆盖义务拦截与义务清单、到点清除解除义务、停用后旧密钥 401、同名不可
重注册、停用账号不能再被指定为见证人）。

### 需见证人确认的预封存（pre-seal proposal）

封存人希望**先取得三名见证人确认、再正式封存**时，可发起预封存提案：

- `POST /pre-seal-proposals`（需 `X-API-Key`），请求体
  `{"body": "<正文>", "witnesses": ["bob","carol","dave"],
  "confirm_deadline": "<带时区 ISO-8601 未来时刻>"}`。发起规则与直接封存一致：
  三名见证人**互异、均已注册、且不含封存人本人**；`confirm_deadline` 必须
  带时区且晚于当前（不带时区 `422`、已过 `400`）。成功 `201`：

  ```json
  {
    "proposal_id": 1,
    "status": "pending",
    "digest": "270463ae1237111ba6ce2460897332ea3661f003813e3ef92591df7e92891f6d",
    "confirm_deadline": "2026-10-03T16:00:00.000000+00:00",
    "witnesses": ["bob", "carol", "dave"],
    "confirmation_count": 0,
    "required": 3,
    "document_id": null,
    "created_at": "2026-10-02T15:00:00.000000+00:00",
    "resolved_at": null
  }
  ```

  响应（及任何错误体）**不含正文**，只给提案编号与正文的 SHA-256 摘要，
  由封存人把摘要与提案编号转交给三名见证人。
- **仅发起人本人与三名受邀见证人**可凭编号查看
  `GET /pre-seal-proposals/{id}`（摘要、截止时刻、状态、确认人数、完成后的
  资料编号；始终不含正文）。其他账号（含凭任意编号试探）与不存在编号统一
  `403` `{"detail":"无权访问该预封存提案或提案不存在"}`，无法探知提案存在性；
  无凭据 `401`。
- `POST /pre-seal-proposals/{id}/confirmations`（需受邀见证人本人密钥），
  请求体 `{"digest": "<提案摘要>"}`。见证人凭摘要各确认一次：
  - **摘要不符** → `409` 拒绝，不计票（本人之后提交正确摘要仍可确认）；
  - **同一见证人重复确认** → `409`，只计一次（预检 + `UNIQUE(proposal_id,
    witness_id)` 双保险，并发重复也至多一票）；
  - 非受邀见证人、发起人本人、其他账号或不存在提案 → 统一 `403`。
  - 前两票响应为 `{"status":"pending","confirmation_count":1|2,
    "document_id":null,…}`；**第三名不同见证人确认后**，在该确认的**同一
    `BEGIN IMMEDIATE` 写事务内**按“现有封存规则”生成一份普通封存资料
    （`documents` 行、三名见证人、一条 `seal` 审计——与 `POST /documents`
    完全一致），响应 `status:"confirmed"` 并返回 `document_id`。
- **重复请求不再生成资料**：提案一旦 `confirmed`，任何确认（含原见证人、
  发起人）都返回 `409`（发起人/非见证人仍为 `403`），撤回也 `409`；
  `pre_seal_proposals.document_id` 固定指向唯一一份资料，库中不会出现第二份。
- `POST /pre-seal-proposals/{id}/revoke`（仅发起人）：在完成（或到期）前
  撤回；非发起人统一 `403`；已完成/已撤回 `409`。
- **到期必失效**：截止时刻未集齐三人确认，提案置 `expired`。后台扫描器
  每秒一轮处理；每个提案写事务（确认/撤回）与提案查询进入临界区时也都先做
  确定性到期判定——即使扫描器停机，重启时的恢复扫描或之后任意参与方的请求
  都会补结算。已失效提案的迟到确认、撤回一律 `409`，不产生资料。
- **截止判定以取得写事务后的实际时刻为准**：发起、确认、撤回都可能在截止前
  到达、等待写锁期间越过截止时刻。三个写接口都在 `BEGIN IMMEDIATE` 取锁
  （并先在锁内复核当前密钥）**之后**才取“当前时刻”做判定——
  - **发起**：取得写锁时截止已过 → 沿用 `400` 拒绝并整体回滚，不落提案；
  - **确认/撤回**：取得写锁时截止已过 → 先把待处理提案确定性失效并清空
    正文（随该事务提交），再沿用 `409` 拒绝，不计新票、不生成资料或封存审计；
  - 截止前取得写锁的有效第三票仍在同一事务生成唯一资料，记录的完成时刻
    （`resolved_at`、资料 `created_at`）即取锁后的实际时刻，不会记下
    早于取锁时刻的时间。
- **正文最小化保留**：提案正文只在 `pending` 期间存于
  `pre_seal_proposals.body`；**撤回、失效或完成后立即清空**（置空串，WAL
  持久化）。成功生成的封存资料其正文按现有封存规则保留在 `documents`
  （仍受开封/清除规则保护）；提案本身的任何接口在任何状态下都不返回正文。
- **并发唯一结果**：确认、撤回、到期结算与密钥轮换共用同一把写锁，按已提交
  顺序产生唯一结果——第三票确认与撤回赛跑，要么 `confirmed`（生成一份资料）
  要么 `revoked`（不生成），互斥且唯一；**轮换先提交**则持旧密钥的确认/撤回
  一律 `401` 并整体回滚（不生成资料、不写 `seal` 审计、不触发到期结算写入，
  旧密钥的读请求同样不得借读路径完成到期写入），**业务写先提交**则照常生成
  资料，随后轮换完成。
- 生成的资料与直接封存**完全同质**：封存人可对其提交开封请求、两票开封、
  替换见证人、交接、安排清除、签发阅览凭证与审计快照；审计链从 `seal`
  开始，快照与既有格式一致（不含预存流程信息）。直接封存接口与
  `X-API-Key` 认证约定保持可用、行为不变。

```bash
# 发起预封存（响应只有 proposal_id 与 digest，无正文）
DEADLINE=$(python3 -c "import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=24)).isoformat())")
curl -sS -X POST $BASE/pre-seal-proposals \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d "{\"body\":\"遗嘱正文……\",\"witnesses\":[\"bob\",\"carol\",\"dave\"],\"confirm_deadline\":\"$DEADLINE\"}"
# -> {"proposal_id":1,"status":"pending","digest":"270463…","confirmation_count":0,"document_id":null,...}

# 发起人/见证人查看；其他账号（含不存在编号）统一 403
curl -sS $BASE/pre-seal-proposals/1 -H "X-API-Key: $OWNER_KEY"
curl -sS -o /dev/null -w '%{http_code}\n' $BASE/pre-seal-proposals/1 -H "X-API-Key: $OTHER_KEY"
# -> 403

# 三名见证人各凭摘要确认一次（重复确认/摘要不符 409 且不计票）
curl -sS -X POST $BASE/pre-seal-proposals/1/confirmations -H "X-API-Key: $W1_KEY" \
  -H 'Content-Type: application/json' -d '{"digest":"270463…"}'
# -> {"status":"pending","confirmation_count":1,"document_id":null,...}
curl -sS -X POST $BASE/pre-seal-proposals/1/confirmations -H "X-API-Key: $W2_KEY" \
  -H 'Content-Type: application/json' -d '{"digest":"270463…"}'
curl -sS -X POST $BASE/pre-seal-proposals/1/confirmations -H "X-API-Key: $W3_KEY" \
  -H 'Content-Type: application/json' -d '{"digest":"270463…"}'
# -> {"status":"confirmed","confirmation_count":3,"document_id":7,...}

# 生成的资料沿用现有接口（GET /documents/7、open-requests、audit、audit-snapshots …）
curl -sS $BASE/documents/7 -H "X-API-Key: $OWNER_KEY"   # sealed，无 body

# 完成前撤回（撤回/失效/完成后提案正文立即清空）
curl -sS -X POST $BASE/pre-seal-proposals/2/revoke -H "X-API-Key: $OWNER_KEY"
# -> {"proposal_id":2,"status":"revoked",...}

# 拒绝示例（均不改状态、不泄露正文/存在性）：
#   三人不互异/含本人 400；见证人未注册 400；截止时刻已过 400；不带时区 422；
#   非参与方查看/确认、非发起人撤回、凭不存在编号 -> 403；无/错密钥 -> 401；
#   摘要不符、重复确认、已完成再确认/撤回、已撤回/已失效再操作、到期后迟到 -> 409。
```

可运行的端到端示例：`python examples/demo_pre_seal.py`
（覆盖发起校验与无正文响应、参与方可见性、摘要不符/重复确认不计票、三人齐
同事务生成资料并沿用两票开封、完成前撤回、到期失效与正文清空）。

### 一次性阅览凭证（view pass）

当前封存人需要把**已开封**资料的正文临时交给一名**没有服务账号**的外部复核人时，
可凭资料编号与一个带时区的阅览截止时刻签发**一次性阅览凭证**：

- `POST /documents/{id}/view-passes`（需 `X-API-Key`，仅当前封存人），请求体
  `{"expires_at": "<带时区 ISO-8601 时刻>"}`。要求：
  - 资料必须**已开封**（未开封 `409`，凭证只用于临时外发已开封正文）；
  - 资料必须**已安排到期清除**（未安排 `409`）；
  - `expires_at` 必须带时区、**晚于当前时间**（`422`/`400`）、且**严格早于
    已安排的清除时刻 `purge_at`**（同刻或更晚 `400`）——凭证最迟在清除前失效，
    清除一旦发生，任何凭证都不可能再读到正文。
- 凭证明文 `token` 是 256 位不可猜测的 URL-safe 随机串，**数据库只存它的
  SHA-256 摘要**；明文**仅在签发响应中返回这一次**，服务端之后无法找回。
  响应同时给出供撤销使用的凭证编号 `pass_id`：

  ```json
  {
    "pass_id": 7,
    "document_id": 1,
    "status": "active",
    "token": "6YPoJDjRrg5yLx4fxwUNc-uIoXHZT-nhH_i1N458keQ",
    "expires_at": "2026-10-01T14:00:00+00:00",
    "purge_at": "2026-10-02T00:00:00+00:00",
    "created_at": "2026-10-01T13:00:00+00:00"
  }
  ```

  封存人应通过安全渠道把 `pass_id`（供自己撤销）与 `token`（供复核人兑换）
  一次性转交。
- `POST /view-passes/redeem`，请求体 `{"token": "<明文>"}`，**无需任何账号
  凭据**（不带 `X-API-Key`；带任意值也不影响）。复核人在截止前可读取**一次**
  正文与摘要，成功响应：

  ```json
  {
    "pass_id": 7,
    "document_id": 1,
    "status": "redeemed",
    "body": "……正文……",
    "digest": "ab12…",
    "expires_at": "2026-10-01T14:00:00+00:00",
    "redeemed_at": "2026-10-01T13:05:00+00:00"
  }
  ```

  兑换在 `BEGIN IMMEDIATE` 写事务内以条件更新
  （`UPDATE … SET status='redeemed' WHERE id=? AND status='active'`）原子翻转
  状态，**并发兑换至多一次成功**，其余统一 `404`。
- `POST /view-passes/{pass_id}/revoke`（需 `X-API-Key`，仅当前封存人）：
  在**兑换前**撤销；已兑付、已撤销、已过阅览截止时刻或资料已清除均 `409`，
  非封存人/编号不存在统一 `403`。
- **清除优先、同事务结算**：兑换与撤销进入写临界区后，先在同一事务内对该资料
  做确定性到点清除判定。**清除时刻与兑换同刻时清除优先**——清除（清空正文、
  状态 `purged`）随该事务先提交落库，兑换随后统一 `404` 且绝不返回正文，
  也不会留下兑付记录。封存人事后把清除改期到凭证截止之前，到期同样生效。
- **统一拒绝、不泄露存在性**：无效明文、过期、已撤销、已兑付、资料已清除的
  兑换全部返回同一个 `404`
  `{"detail": "凭证无效、已过期、已撤销、已兑付，或资料已清除"}`，
  无法据此区分凭证或资料是否存在；失败兑换**不写任何记录**。
- **审计**：签发、成功兑换、撤销分别追加 `view_pass_issued`、
  `view_pass_redeemed`（`actor` 为 `null`，因外部复核人无账号）、
  `view_pass_revoked` 到原有 `audit_log`，并随资料的审计快照一起可被离线验真；
  快照仍不含正文。
- 现有参与方读权限、清除规则与审计快照约定保持不变；凭证不改变资料状态
  （资料不因签发/兑换而开封或清除）。

```bash
# 前提：资料已两票开封，且已安排未来清除时刻
# 封存人签发（截止时刻取 1 小时后，必须早于 purge_at）
EXPIRES=$(python3 -c "import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=1)).isoformat())")
curl -sS -X POST $BASE/documents/1/view-passes \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d "{\"expires_at\":\"$EXPIRES\"}"
# -> {"pass_id":7,"status":"active","token":"6YPo…（仅显示这一次）","expires_at":"…","purge_at":"…",...}

# 外部复核人：无需 X-API-Key，凭明文兑换（仅一次）
curl -sS -X POST $BASE/view-passes/redeem -H 'Content-Type: application/json' \
  -d '{"token":"6YPoJDjRrg5yLx4fxwUNc-uIoXHZT-nhH_i1N458keQ"}'
# -> {"status":"redeemed","body":"……正文……","digest":"ab12…",...}

# 再次兑换 / 伪造明文 / 过期 / 撤销后 / 已清除 —— 全部同一个 404：
# {"detail":"凭证无效、已过期、已撤销、已兑付，或资料已清除"}

# 封存人凭凭证编号在兑换前撤销
curl -sS -X POST $BASE/view-passes/7/revoke -H "X-API-Key: $OWNER_KEY"
# -> {"pass_id":7,"status":"revoked","revoked_at":"…"}
```

可运行的端到端示例：`python examples/demo_view_pass.py`
（默认 `http://127.0.0.1:8000`，可传入自定义地址）。

### 站内通知（notifications）

资料发生四类状态变更时，系统在**状态变更的同一写事务内**向当时的参与方各投递
一条站内通知，收件人按变更时刻的名单在事务内固定：

| 事件 `event_type` | 触发时机 | 收件人 |
| --- | --- | --- |
| `opened` | 资料开封（第二票即时开封，或到点自动开封/清除结算中补开） | 当时的封存人 + 三名见证人 |
| `purged` | 到期清除生效（扫描器、写事务或资料查询确定性结算） | 当时的封存人 + 三名见证人 |
| `witness_replaced` | 封存人成功替换一名指定见证人 | 封存人 + 旧见证人 + 新见证人（另两名见证人也各收到一条） |
| `handover_confirmed` | 受让人确认交接、封存人身份转移 | 新旧封存人 + 三名见证人 |

- **事件序号**：每条通知带 `event_seq`，等于该状态变更主审计记录（`opened` /
  `purged` / `witness_replaced` / `handover_confirmed`）在 `audit_log` 上的
  全局自增序号。同一事件的所有收件人通知共享同一 `event_seq`；列表按
  `(event_seq, id)` 升序，故通知顺序与审计链上的事件顺序一致。
- **最小内容**：通知只有 `notification_id`、`event_seq`、`document_id`、
  `event_type`、`at`、`is_read`，**不含正文、阅览凭证、签名或任何密钥**。
  通知本身不进入审计链，标记已读也不写审计。
- **同事务、不重复、失败不留**：通知与状态变更在同一 `BEGIN IMMEDIATE`
  事务内写入——变更回滚则通知一并回滚（失败/被拒操作不留通知），变更提交则
  通知必已存在；`UNIQUE(recipient_id, event_seq)` 与同事务写入共同保证后台
  扫描器与请求并发结算同一变化（同一次到点开封/清除）时每名收件人至多一条。
- **失权仍可收件**：被替换掉的旧见证人、交接后的旧封存人随即失去资料接口
  权限（仍统一 `403`），但仍可列出、翻页、标记自己收到的通知；通知不提供
  任何资料内容，不能借此访问资料。

读取本人通知（需 `X-API-Key`）：

- `GET /notifications?cursor=<事件序号>&page_size=<页大小>`
  - `cursor`：非负整数，默认 `0`；只返回 `event_seq > cursor` 的本人通知
    （游标即“上一页最后一条通知的 `event_seq`”）。负数或非整数 `422`。
  - `page_size`：整数，范围 `1～100`，默认 `20`；越界 `422`。
  - 成功 `200`：

    ```json
    {
      "notifications": [
        {"notification_id": 5, "event_seq": 12, "document_id": 2,
         "event_type": "witness_replaced",
         "at": "2026-10-02T01:24:58.763282+00:00", "is_read": false}
      ],
      "next_cursor": 12
    }
    ```

    `next_cursor` 为本页最后一条通知的事件序号，用它作为下一次请求的
    `cursor` 即可继续翻页；本页之后无更多通知时 `next_cursor` 为 `null`。

标记已读（需 `X-API-Key`）：

- `POST /notifications/{notification_id}/read`，无请求体。把本人名下该通知
  置为已读并返回 `{"notification_id", "is_read": true, "read_at": "…"}`。
  - 重复标记幂等：`read_at` 保留**首次**标记时刻，不产生任何额外记录；
  - 标记他人通知或不存在的编号统一 `403`
    `{"detail": "无权操作该通知或通知不存在"}`，不区分是否存在，且不改状态；
  - 缺少/错误 `X-API-Key` 为 `401`。

```bash
# 第一页（页大小 20，从事件序号 0 之后开始）
curl -sS "$BASE/notifications?cursor=0&page_size=20" -H "X-API-Key: $OWNER_KEY"
# -> {"notifications":[{"notification_id":1,"event_seq":10,"document_id":1,
#     "event_type":"opened","at":"…","is_read":false}, …],
#     "next_cursor":12}

# 用 next_cursor 翻下一页
curl -sS "$BASE/notifications?cursor=12&page_size=20" -H "X-API-Key: $OWNER_KEY"

# 标记一条本人通知为已读（重复调用幂等，read_at 不变）
curl -sS -X POST "$BASE/notifications/1/read" -H "X-API-Key: $OWNER_KEY"
# -> {"notification_id":1,"is_read":true,"read_at":"…"}

# 拒绝示例（均不改状态）：
#   cursor=-1 / page_size=0 / page_size=101 -> 422；
#   标记他人或不存在的通知编号 -> 403；无/错 X-API-Key -> 401。
```

可运行的端到端示例：`python examples/demo_notifications.py`
（覆盖四类事件投递、失权后仍可读通知、游标分页、标记已读幂等、越权与非法
参数拒绝、到期清除去重）。

### 审计快照签发与离线验真（audit snapshot）

资料参与方需要把封存过程交给离线复核人核验时，**当前封存人或当前指定见证人**
可在到点开封、清除等结算完成后，为某份资料签发一份**审计快照**。快照包含
资料编号、摘要、当前状态与开封/清除时刻、按原顺序排列且标明截止序号
（`audit_upto`）的审计记录，**不含正文，也不含任何密钥**。

- `POST /documents/{id}/audit-snapshots`（需 `X-API-Key`，仅当前封存人或当前
  指定见证人）：签发在同一写事务内先完成到点开封/到期清除等**确定性结算**，
  再按该事务的一致视图取数并签名——快照始终对应一个**已提交的一致状态**，
  不会把并发操作后的状态与之前的审计拼接。签发动作本身追加一条
  `audit_snapshot_issued` 审计记录（截止序号之前的内容即本次快照）。
  响应 `201`：

  ```json
  {
    "snapshot": {
      "version": 1,
      "document_id": 1,
      "digest": "ab12…",
      "status": "opened",
      "created_at": "2026-10-01T08:00:00+00:00",
      "opened_at": "2026-10-01T09:00:00+00:00",
      "purge_at": null,
      "purged_at": null,
      "audit_upto": 9,
      "records": [
        {"seq": 3, "at": "…", "action": "seal", "actor": "alice", "request_id": null, "detail": "…"},
        {"seq": 9, "at": "…", "action": "opened", "actor": null, "request_id": 1, "detail": "…"}
      ]
    },
    "signature": "rqHz4GK8…（base64，64 字节 Ed25519 签名）",
    "key_id": "92e97970ddaf5999",
    "algorithm": "ed25519",
    "canonicalization": "canonical-json-v1"
  }
  ```

- 签名密钥为服务端持有的 **Ed25519** 密钥（纯标准库实现，RFC 8032，无任何
  第三方依赖），持久化在 `signing_keys` 表（随库备份/重启保留），**重启后
  旧签名仍可验证**；`key_id` 为公钥 SPKI DER 的 SHA-256 前 16 位十六进制。
  快照按 `canonical-json-v1` 规范化后签名：`json.dumps(obj, ensure_ascii=True,
  sort_keys=True, separators=(",", ":"))` 的 ASCII 字节串——键序无关、记录
  数组顺序敏感，复核方可用任意语言独立重放验证。
- `GET /audit-snapshots/public-key`（无需凭据）：返回当前签名公钥（PEM，
  标准 Ed25519 SPKI，OpenSSL/`cryptography` 可直接加载）与 `key_id`，
  供复核人**事先**通过可信渠道取得并核对指纹后建立信任。
- `POST /audit-snapshots/verify`（无需凭据）：复核人提交
  `{"snapshot": <快照对象>, "signature": "<base64 签名>", "public_key": "<事先信任的公钥>"}`。
  验真**只使用随请求提交的公钥**，绝不从快照包内取公钥建立信任（快照结构
  严格校验，夹带多余字段直接 `422`）。成功 `200`：

  ```json
  {"valid": true, "document_id": 1, "status": "opened",
   "audit_upto": 9, "record_count": 5, "key_id": "92e97970ddaf5999",
   "algorithm": "ed25519"}
  ```

- **明确失败**：快照字段值或审计记录顺序被篡改、签名不匹配 → `400`
  `{"detail": "签名不匹配：…"}`；`version` 不是受支持版本 → `400`
  `{"detail": "不支持的快照版本: …"}`；公钥无法解析为有效 Ed25519 公钥
  （垃圾串、长度/前缀不符、曲线外的点）→ `400` `{"detail": "公钥无效：…"}`；
  快照缺字段/多字段/类型不符 → `422`（错误体不回显提交内容）。
- 交接后**旧封存人不得再签发**（统一 `403`）；被替换的旧见证人同样失去
  签发权；非参与方与不存在资料沿用统一 `403` 语义，不区分资料是否存在。

```bash
# 复核人事先取得签名公钥并建立信任（实际场景应通过可信渠道核对 key_id）
curl -sS $BASE/audit-snapshots/public-key
# -> {"algorithm":"ed25519","key_id":"92e97970ddaf5999","public_key":"-----BEGIN PUBLIC KEY-----…"}

# 封存人（或任一当前指定见证人）签发审计快照
curl -sS -X POST $BASE/documents/1/audit-snapshots -H "X-API-Key: $OWNER_KEY" > snapshot_pkg.json
# -> {"snapshot":{...,"audit_upto":9,"records":[...]},"signature":"…","key_id":"…",...}

# 复核人离线验真：快照 + 签名 + 事先信任的公钥
python3 - <<'EOF' > verify_req.json
import json
pkg = json.load(open("snapshot_pkg.json"))
pem = open("trusted_pubkey.pem").read()   # 事先信任渠道获得的公钥
print(json.dumps({"snapshot": pkg["snapshot"], "signature": pkg["signature"], "public_key": pem}))
EOF
curl -sS -X POST $BASE/audit-snapshots/verify \
  -H 'Content-Type: application/json' -d @verify_req.json
# -> {"valid":true,"audit_upto":9,"document_id":1,"status":"opened",...}

# 篡改任一字段或调换 records 顺序 -> 400 签名不匹配；version 改 2 -> 400 版本不支持；
# 公钥换成别的有效 Ed25519 公钥 -> 400 签名不匹配（信任只来自事先信任的公钥）
```

### 调用示例（curl）

```bash
BASE=http://127.0.0.1:8000

# 注册（api_key 只显示这一次，请保存）
curl -sS -X POST $BASE/register -H 'Content-Type: application/json' \
  -d '{"username":"alice"}'
curl -sS -X POST $BASE/register -H 'Content-Type: application/json' -d '{"username":"bob"}'
curl -sS -X POST $BASE/register -H 'Content-Type: application/json' -d '{"username":"carol"}'
curl -sS -X POST $BASE/register -H 'Content-Type: application/json' -d '{"username":"dave"}'

# 封存
curl -sS -X POST $BASE/documents \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d '{"body":"遗嘱正文……","witnesses":["bob","carol","dave"]}'
# -> {"document_id":1,"status":"sealed","digest":"ab12…","witnesses":[...]}

# 开封前查询（无 body）
curl -sS $BASE/documents/1 -H "X-API-Key: $OWNER_KEY"

# 提交开封请求（截止时刻取 24 小时后）
DEADLINE=$(python3 -c "import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=24)).isoformat())")
curl -sS -X POST $BASE/documents/1/open-requests \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d "{\"reason\":\"家庭会议\",\"deadline\":\"$DEADLINE\"}"
# -> {"request_id":1,"status":"pending",...}

# 两名见证人分别同意，digest 必须是封存响应里的摘要
curl -sS -X POST $BASE/open-requests/1/approve \
  -H "X-API-Key: $W1_KEY" -H 'Content-Type: application/json' \
  -d '{"digest":"ab12…"}'
# -> {"approval_count":1,"status":"pending"}
curl -sS -X POST $BASE/open-requests/1/approve \
  -H "X-API-Key: $W2_KEY" -H 'Content-Type: application/json' \
  -d '{"digest":"ab12…"}'
# -> {"approval_count":2,"status":"opened"}

# 开封后查询：body / digest / reason / witness_approvals 全部可见
curl -sS $BASE/documents/1 -H "X-API-Key: $OWNER_KEY"

# 操作记录（按时间顺序）
curl -sS $BASE/documents/1/audit -H "X-API-Key: $OWNER_KEY"
```

可直接运行的端到端示例（服务启动后）：

```bash
bash examples/demo.sh                    # curl 版（未设最早开封时刻的基础流程）
python3 examples/demo.py                 # 纯标准库 Python 版
python3 examples/demo_scheduled.py       # 最早开封时刻：waiting -> 自动开封；补票立即开封
python3 examples/demo_replace_witness.py # 见证人替换：直接换人 / 退票补票 / waiting 退回待同意
python3 examples/demo_handover.py        # 封存人交接：邀请/撤回/逾期/确认失效旧请求/转移身份/开封后邀请失效
python3 examples/demo_purge.py           # 到期清除：安排/改期/取消/到点清除/连带失效/时序规则/交接后管理权
python3 examples/demo_audit_snapshot.py  # 审计快照：签发/验真/篡改失败/版本与公钥失败/交接后旧封存人禁签
python3 examples/demo_view_pass.py       # 一次性阅览凭证：签发/无账号兑换一次/再兑换404/兑换前撤销
python3 examples/demo_notifications.py   # 站内通知：开封/替换/交接/清除投递、游标分页、标记已读与拒绝场景
python3 examples/demo_api_key_rotation.py # 密钥轮换：一次性返回/旧密钥即时持久失效/401同文案/未轮换与无凭据接口不变
python3 examples/demo_pre_seal.py        # 预封存：三人凭摘要确认同事务生成资料/重复不计票/撤回/到期失效/正文清空
```

### 典型拒绝场景

```bash
# 错误密钥 -> 401，不产生任何记录
curl -sS -o /dev/null -w '%{http_code}\n' $BASE/me -H 'X-API-Key: wrong'

# 轮换后再用旧密钥（含业务写）-> 同样 401 同一文案，事务回滚、不留业务审计
curl -sS -o /dev/null -w '%{http_code}\n' -X POST $BASE/me/api-key/rotate -H "X-API-Key: $KEY"
curl -sS -o /dev/null -w '%{http_code}\n' $BASE/me -H "X-API-Key: $KEY"   # 401

# 非见证人同意 -> 403；摘要不一致 -> 409；截止后同意 -> 409；
# 同一见证人再次同意 -> 409（只计一次）；开封后再提请求 -> 409（重复开封）
```

## 运行测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

测试覆盖：注册与密钥、封存校验、两票开封全流程、重复同意、撤销后旧同意失效、
到期失效、最早开封时刻校验、`waiting` 待开封与到点自动开封、到点时票不足补齐
立即开封、迟到撤销/迟到扫描不能作废已到点开封、停机期间到点的重启恢复、
开封前正文零泄露、审计顺序，以及基于真实 uvicorn + 多线程的
**并发同意/同意撤销赛跑**（验证只产生一个已提交终态）和**重启一致性**；
另含见证人替换：直接换人后旧见证人失去权限、进行中请求退票（被替换者同意失效、
其他同意保留）、新见证人凭摘要补票开封、`waiting` 替换投票者退回 `pending`
（替换未投票者维持 `waiting`）、到点两票已齐先开封并拒绝替换、过截止请求先
按原规则失效再换人、替换后冷读库重启一致，以及**并发替换 vs 第二票同意赛跑**
（验证锁内以当前名单复核身份、只产生一个终态）；
再含封存人交接：发起校验（受让人已注册/非本人/非见证人、截止时刻带时区且在未来）、
每份资料唯一待确认邀请、撤回与重新邀请、逾期不能确认且可重新邀请、确认转移身份
（原封存人随即 403、见证人名单/正文/摘要不变）、旧开封请求 `invalidated` 与旧同意
清零、过截止旧请求按原规则 `expired`、`waiting` 请求随交接失效且不开封、到点两票
已齐先开封并拒绝交接、资料开封连带邀请失效、交接结果冷读库一致，以及真实 uvicorn +
多线程的**并发确认 vs 撤回、并发确认 vs 第二票同意赛跑**（两种终态互斥且唯一，
成功交接与请求失效均留审计）；
最后含到期清除：安排/改期/取消与权限校验（仅当前封存人、时刻带时区且在未来）、
扫描器到点清除与无扫描器时查询/写事务的确定性清除、清除后任何接口不返回正文且
库中正文已清空、未开封资料清除连带（进行中请求 `invalidated`、旧同意删除、待确认
邀请 `expired`）、已开封资料按期清除（摘要/开封时刻/见证记录保留）、同刻清除优先、
开封更早先开后清（含延迟处理补开）、清除更早则不开封、已清除资料全部写操作 `409`、
已到期的改期/取消被拒且清除先生效、交接后清除管理权转移、停机期间到点的重启恢复、
冷读库一致、旧库（无 purge 列/旧 CHECK 约束）幂等迁移，以及真实 uvicorn + 多线程的
**并发确认 + 同意 vs 到点清除**（两者均被拒，唯一 `purged` 终态）；
另含审计快照：签发权限（仅当前封存人/当前指定见证人，交接后旧封存人与被替换
见证人 403、非参与方与不存在资料统一 403）、快照内容（编号/摘要/状态与开封/
清除时刻/按序审计记录与截止序号，不含正文与密钥）、签发事务内先完成到点开封
与到期清除结算、签发留痕、篡改字段/调换记录顺序/版本不支持/公钥无效/错公钥/
夹带公钥自封包全部明确失败、键序无关验真、重启后旧签名仍可验证、真实 uvicorn +
多线程的**并发签发 vs 第二票同意**（快照始终对应一个已提交的一致状态），
以及 Ed25519 纯标准库实现的 RFC 8032 官方测试向量；
还含一次性阅览凭证：签发前置条件（已开封、已安排清除、截止时刻带时区且在未来
并严格早于清除时刻、仅当前封存人）、库内只存 SHA-256 摘要而明文仅签发返回、
无凭据兑换一次正文与摘要、并发兑换至多一次成功、兑换前撤销（权限/已兑付/已过期
各路径）、过期不改状态且失败兑换不留痕、兑换/撤销临界区同事务结算到点清除
（同刻清除优先、清除先提交再拒绝）、已清除资料兑换统一 404 不泄露正文或存在性、
签发/兑换/撤销写入原审计链并进入快照，以及 TestClient 多线程的
**并发兑换、兑换 vs 撤销赛跑**（只产生一个终态）。
站内通知测试覆盖：开封（即时/到点自动）与到期清除（扫描器/写事务/读路径
三种结算）向当时封存人 + 三名见证人各投递一条、替换见证人通知封存人与新旧
见证人、交接确认通知新旧封存人与见证人、收件人随变更时刻名单固定（替换后
开封旧见证人不收、交接后开封旧封存人不收）、`event_seq` 取主审计序号且
升序排列、通知只含最小字段不含正文/摘要、失败与回滚操作不留通知、非负游标
与 1～100 页大小分页及边界/非法参数 `422`、按编号标记已读幂等（`read_at`
保留首次）、他人/不存在编号统一 `403` 不改状态、缺凭据 `401`、失去资料
权限后仍可读并标记本人通知但不能借此访问资料，以及真实 uvicorn + 多线程的
**多请求并发结算同一次到点清除**（每名参与方恰好一条 purged，不重复投递）；
另含 **API Key 轮换**：新密钥仅一次性返回、库内仅存摘要（明文不落盘含 WAL）、
旧密钥轮换提交后读/写立即 `401` 且重启后仍失效、再次轮换使前一密钥失效、
轮换留 `api_key_rotated` 审计而被拒旧密钥业务写不留任何业务审计、无效与已
失效密钥同一 `401` 文案不泄露账号存在性、未轮换账号与无凭据兑换/验真接口
行为不变，以及真实 uvicorn + 多线程的**同密钥并发轮换只产生一个赢家**、
**轮换先提交则旧密钥封存 401 回滚**、**业务写先提交则封存落库后轮换完成**、
**旧密钥读请求不得触发到点清除写入**四类并发/时序用例。
预封存提案测试覆盖：发起校验（三人互异/已注册/非本人、截止时刻带时区且在未来）、
响应不含正文、仅发起人/受邀见证人可见（其他账号与不存在编号统一 403）、
摘要不符与重复确认 409 且不计票、非受邀见证人/发起人本人不能确认、非发起人不能
撤回、三人在截止前确认后在同一事务生成一份普通封存资料（`seal` 审计与直接封存
一致、见证人名单一致、开封前无正文）、已完成提案重复确认/撤回不再生成资料、
完成前撤回后不能确认、到期扫描器失效与无扫描器时查询/确认事务的确定性失效、
迟到第三票不产生资料、撤回/失效/完成后库中提案正文清空、生成资料沿用两票开封与
审计快照、旧密钥轮换先提交则确认/撤回 401 回滚（无资料、无封存审计）、旧密钥
读请求不得触发到期写入，以及**截止判定以取得写锁后的实际时刻为准**（占用写锁
越过截止时刻：发起 400 不落提案、第三票确认先失效清正文再 409 不计票不生成
资料、撤回先失效再 409；截止前取得写锁的第三票仍同事务生成唯一资料且完成时刻
不早于取锁时刻），以及真实 uvicorn + 多线程的**第三票/撤回并发唯一终态**、
**第三票 vs 密钥轮换并发按提交顺序唯一结果**与**停机期间到期重启恢复**。

## 项目结构

```
app/
  config.py     # 环境变量配置
  db.py         # SQLite 连接、建表与旧库幂等迁移、BEGIN IMMEDIATE 写事务、时间规范
  schemas.py    # 请求模型（含带时区 deadline / earliest_open_at、替换见证人、交接邀请、清除时刻、阅览凭证、预封存提案/确认、快照验真入参校验）
  ed25519.py    # Ed25519 签名（RFC 8032）纯标准库实现，无第三方依赖
  snapshots.py  # 审计快照：canonical-json-v1 规范化、签名密钥持久化、公钥 PEM/指纹、快照组装与签名
  main.py       # FastAPI 路由、认证、定时开封+到期清除+预封存到期+过期扫描器、全部业务规则（见证人替换、封存人交接、到期清除、审计快照签发与验真、一次性阅览凭证签发/兑换/撤销、站内通知投递/游标分页/标记已读、API Key 轮换与写锁内密钥复核、预封存提案发起/查看/确认/撤回与三人齐同事务生成资料、账号停用前到点结算与未了义务核对）
init_db.py      # 幂等初始化入口
entrypoint.sh   # 容器入口（初始化 + uvicorn）
examples/       # demo.sh / demo.py / demo_scheduled.py / demo_replace_witness.py / demo_handover.py / demo_purge.py / demo_audit_snapshot.py / demo_view_pass.py / demo_notifications.py / demo_api_key_rotation.py / demo_pre_seal.py / demo_account_deactivation.py 端到端示例
tests/          # pytest 测试（功能、定时、替换见证人、交接、到期清除、审计快照、阅览凭证、站内通知、密钥轮换、预封存提案、账号停用、并发、重启）
Dockerfile
docker-compose.yml
```
