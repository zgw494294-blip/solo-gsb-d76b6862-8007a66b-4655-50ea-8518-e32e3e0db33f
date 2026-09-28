# 双端消息协议死锁分析 API

离线分析一个双端（A、B）消息传递协议是否存在死锁。服务接收两端的有限状态机与
send/receive 转移定义，**穷举全部可达状态**，返回是否存在死锁、访问状态数；若存在
死锁，返回**最短**的转移序列（同长度按转移 id 序列字典序取最小）及每一步执行后的
状态与队列快照。

## 协议模型与语义

- 两端 A、B 各自拥有 1～8 个状态、一个初态和一个非空终态集。
- 转移 `{id, type, from, to, message}`：
  - `id` 全局唯一；`type` 为 `send` 或 `receive`。
  - 转移归属方由 `from` 决定：`from` 属于 A 的状态集则该转移属于 A，属于 B 则属于 B；
    `to` 必须属于同一方。
- 两个方向各有一条**容量为 1** 的队列：`A_to_B` 与 `B_to_A`。
  - `send`：仅当**本方向**队列为空时可执行，执行后消息入本方向队列。
  - `receive`：仅当**反方向**队首消息与转移的 `message` 相等时可执行，执行后该消息出队。
- 全局状态 = (A 当前状态, B 当前状态, A_to_B 队列, B_to_A 队列)，初态两队列为空，
  两端转移可任意交错执行。
- **完成态**：两端同时处于终态且两条队列均为空。完成态不再扩展（即使还有出边）。
- **死锁态**：非完成态且没有任何可执行的转移（包括队列中卡着无法被接收的消息的情形）。
- 搜索方式：从初态做广度优先遍历，每个状态的可行后继按转移 id 字典序展开。
  第一个遇到的死锁态即对应**最短**见证路径；同长度时该顺序保证 id 序列字典序最小
  （id 比较采用 Python 字符串序，即逐字符 Unicode 码点序）。

## 快速开始（单条命令）

需要 Docker 与 Docker Compose。在仓库根目录执行：

```bash
docker compose up --build
```

服务监听 **8000** 端口：

- `POST /v1/deadlocks` — 死锁分析
- `GET  /health` — 健康检查
- `GET  /docs` — Swagger UI（交互式文档）

不使用 Compose 时也可以：

```bash
docker build -t deadlock-api .
docker run --rm -p 8000:8000 deadlock-api
```

## API

### `POST /v1/deadlocks`

请求体：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `A.states` / `B.states` | `string[]` | 1～8 个，互不重复，非空字符串 |
| `A.initial` / `B.initial` | `string` | 必须属于该方 `states` |
| `A.finals` / `B.finals` | `string[]` | 非空，元素必须属于该方 `states` |
| `transitions` | `object[]` | 总数 ≤ 30 |
| `transitions[].id` | `string` | 全局唯一，非空 |
| `transitions[].type` | `string` | `send` 或 `receive` |
| `transitions[].from` | `string` | 必须属于某一方的状态集（决定归属方） |
| `transitions[].to` | `string` | 必须属于与 `from` 相同一方的状态集 |
| `transitions[].message` | `string` | 非空；全部转移中不同消息种类 ≤ 8 |

响应 `200`：

| 字段 | 说明 |
| --- | --- |
| `deadlock` | 是否存在可达死锁态 |
| `visited_states` | 遍历访问到的可达状态总数（含完成态与死锁态） |
| `witness` | 无死锁时为 `null`；否则为最短（同长字典序最小）见证 |
| `witness.length` | 见证转移序列长度 |
| `witness.transition_ids` | 按执行顺序的转移 id 序列 |
| `witness.initial` | 初始状态与队列快照 |
| `witness.steps[]` | 每步的转移信息及执行后的状态与队列快照（`after`） |
| `witness.deadlock_state` | 最终死锁态快照 |

状态快照格式：`{"states": {"A": ..., "B": ...}, "queues": {"A_to_B": [...], "B_to_A": [...]}}`
（队列容量为 1，故数组长度为 0 或 1）。

错误：非法引用（初态/终态/转移端点指向不存在的状态）、重复 id、状态数/转移数/
消息种类越界、字段缺失或类型错误等，一律返回 `422`，`detail` 中包含具体原因。
另外，同一个状态名不允许同时出现在 A、B 两方（否则转移归属无法确定，同样返回 `422`）；
请在两方使用互不相同的状态名（如 `a0` 与 `b0`）。

### 示例 1：存在死锁

B 只接受消息 `z`，而 A 发出的是 `x`：消息永远卡在队列里，A 也被槽位占满无法继续。

```bash
curl -s -X POST http://localhost:8000/v1/deadlocks \
  -H 'Content-Type: application/json' \
  -d '{
    "A": {"states": ["a0", "a1", "a2"], "initial": "a0", "finals": ["a2"]},
    "B": {"states": ["b0", "b1"], "initial": "b0", "finals": ["b1"]},
    "transitions": [
      {"id": "s1", "type": "send",    "from": "a0", "to": "a1", "message": "x"},
      {"id": "s2", "type": "send",    "from": "a1", "to": "a2", "message": "y"},
      {"id": "r1", "type": "receive", "from": "b0", "to": "b1", "message": "z"}
    ]
  }'
```

响应：

```json
{
  "deadlock": true,
  "visited_states": 2,
  "witness": {
    "length": 1,
    "transition_ids": ["s1"],
    "initial": {
      "states": {"A": "a0", "B": "b0"},
      "queues": {"A_to_B": [], "B_to_A": []}
    },
    "steps": [
      {
        "transition_id": "s1",
        "type": "send",
        "from": "a0",
        "to": "a1",
        "message": "x",
        "after": {
          "states": {"A": "a1", "B": "b0"},
          "queues": {"A_to_B": ["x"], "B_to_A": []}
        }
      }
    ],
    "deadlock_state": {
      "states": {"A": "a1", "B": "b0"},
      "queues": {"A_to_B": ["x"], "B_to_A": []}
    }
  }
}
```

### 示例 2：无死锁（ping-pong 正常完成）

```bash
curl -s -X POST http://localhost:8000/v1/deadlocks \
  -H 'Content-Type: application/json' \
  -d '{
    "A": {"states": ["a0", "a1", "a2"], "initial": "a0", "finals": ["a2"]},
    "B": {"states": ["b0", "b1", "b2"], "initial": "b0", "finals": ["b2"]},
    "transitions": [
      {"id": "t1", "type": "send",    "from": "a0", "to": "a1", "message": "ping"},
      {"id": "t2", "type": "receive", "from": "b0", "to": "b1", "message": "ping"},
      {"id": "t3", "type": "send",    "from": "b1", "to": "b2", "message": "pong"},
      {"id": "t4", "type": "receive", "from": "a1", "to": "a2", "message": "pong"}
    ]
  }'
```

响应：

```json
{"deadlock": false, "visited_states": 5, "witness": null}
```

### 示例 3：非法输入（422）

```bash
curl -s -X POST http://localhost:8000/v1/deadlocks \
  -H 'Content-Type: application/json' \
  -d '{
    "A": {"states": ["a0"], "initial": "a0", "finals": ["a0"]},
    "B": {"states": ["b0"], "initial": "b0", "finals": ["b0"]},
    "transitions": [
      {"id": "t1", "type": "send", "from": "a0", "to": "b0", "message": "m"}
    ]
  }'
```

响应（`to` 指向了对方的状态）：

```json
{"detail": "transition 't1' target state 'b0' does not refer to a state of party A"}
```

## 本地开发与测试

核心分析器 `app/solver.py` 只依赖 Python 标准库，可脱离 Web 框架直接测试：

```bash
python3 tests/test_solver.py
```

覆盖：正常完成、消息卡队列死锁、空队列死锁、初态即死锁、完成态不再扩展、
同长字典序 tie-break、最短长度优先，以及全部 422 校验分支。

## 实现说明

- 状态空间上限为 8×8×9×9 = 5184（每端 8 个状态 × 每队列 8 种消息 + 空），
  配合 ≤30 条转移，BFS 穷举代价极小。
- 字典序保证：每个状态的可行后继按转移 id 排序后入队，BFS 首次访问即固定父指针，
  因此第一个出队的死锁态对应的路径就是「最短、同长字典序最小」的见证。
- 目录结构：

```
app/
  main.py     FastAPI 入口与请求模型（结构校验）
  solver.py   语义校验 + BFS 可达性分析（纯标准库）
tests/
  test_solver.py
Dockerfile
docker-compose.yml
requirements.txt
```
