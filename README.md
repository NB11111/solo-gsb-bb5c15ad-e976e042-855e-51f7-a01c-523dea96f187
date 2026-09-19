# HTTP 语言协商解释 API（Language Negotiation API）

供国际化网关调用的语言协商服务：输入客户端原始 `Accept-Language` 头、服务端支持的
BCP 47 标签列表与默认语言，返回协商选出的语言，并给出**每个候选标签的接受 / 淘汰
完整轨迹**（匹配的范围、q 权重、精确 / 前缀 / 通配符匹配类型、排序名次与原因）。

- Python 3.12 · FastAPI · Pydantic v2
- 语法规范：[RFC 7231 §5.3.5](https://www.rfc-editor.org/rfc/rfc7231#section-5.3.5)（Accept-Language 与 q 值）
- 匹配规范：[RFC 4647](https://www.rfc-editor.org/rfc/rfc4647)（大小写不敏感匹配、前缀截断 Lookup、通配符过滤、q=0 排除）

---

## 一条命令启动

已安装 Docker 与 Compose 插件的环境下，在项目根目录执行：

```bash
docker compose up --build -d
```

服务随即在 **http://localhost:8000** 对外提供：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000/health | 健康检查（返回 `{"status":"ok",...}`） |
| http://localhost:8000/api/v1/negotiate-language | 语言协商接口（`POST`） |
| http://localhost:8000/docs | Swagger 交互式文档 |
| http://localhost:8000/openapi.json | OpenAPI 3 描述文件 |

停止与查看日志：

```bash
docker compose down          # 停止
docker compose logs -f       # 跟踪日志
```

> 不使用 Compose 时也可：`docker build -t language-negotiator . && docker run -p 8000:8000 language-negotiator`

---

## 必要配置

本服务无状态，**不依赖数据库或外部服务**；唯一需要的配置是每个请求中携带的
服务端语言清单与默认语言（见下方请求字段）。运行期可选项通过环境变量调整：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST_PORT` | `8000` | 宿主机映射端口（容器内固定监听 8000），如 `HOST_PORT=18000 docker compose up -d` |
| `UVICORN_WORKERS` | `1` | uvicorn worker 进程数，高并发可按 CPU 调整（如 `4`） |

请求字段（同时接受 `snake_case` 与 `camelCase` 两种命名）：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `accept_language` | string | 是 | 客户端原始 Accept-Language 值；空串 / 纯空白合法，表示"无偏好"，将回退默认语言 |
| `supported_languages` | string[] | 是 | 服务端支持的 BCP 47 标签；**不能为空、不能有重复**；列表顺序即服务端稳定优先级 |
| `default_language` | string | 是 | 回退语言；**必须是 supported_languages 之一**，且为合法 BCP 47 标签 |

---

## 调用示例

### 1) 常规 q 权重 + 截断匹配

```bash
curl -s -X POST http://localhost:8000/api/v1/negotiate-language \
  -H 'Content-Type: application/json' \
  -d '{
        "accept_language": "fr-CH, fr;q=0.9, en;q=0.8, de;q=0.7, *;q=0.5",
        "supported_languages": ["en-US", "fr", "de"],
        "default_language": "en-US"
      }'
```

响应关键字段：

```jsonc
{
  "selectedLanguage": "fr",          // 最终选中
  "selectedSource": "exact",         // exact | prefix | wildcard | default
  "usedFallback": false,             // 是否走了默认语言回退
  "fallbackReason": null,            // no_valid_ranges | no_acceptable_match | all_matches_excluded_q0
  "defaultExplicitlyExcluded": false,// 默认语言是否被显式 q=0 排除
  "parsedRanges": [ /* 每个（合并后的）范围：权重、出现位置、命中标签、q=0 排除的标签 */ ],
  "candidates": [ /* 每个候选标签的完整接受/淘汰轨迹，见下 */ ]
}
```

每个候选（`candidates[]`）：

```jsonc
{
  "tag": "fr",
  "serverPosition": 1,               // 在 supported_languages 中的下标
  "matchedRange": "fr",              // 决定性命中的范围（未命中为 null）
  "matchType": "exact",              // exact | prefix | wildcard | null
  "q": "0.9",
  "specificity": 1,                  // 命中范围的子标签数（越具体越大）
  "considered": true,                // q>0 才有资格入选
  "selected": true,
  "status": "selected",              // selected | eliminated | fallback-selected
  "reasons": ["matched_exact"],      // 轨迹原因，如 lower_client_precedence /
                                     // lost_stable_tie_break:server_list_order /
                                     // excluded_q0:exact|prefix|wildcard /
                                     // no_matching_range / fallback:*
  "selectionRank": 1,
  "evidence": [ /* 该候选对所有范围逐一比对的证据（含通配符被显式范围遮蔽标记） */ ]
}
```

### 2) q=0 排除与通配符

```bash
# 除 en 外都接受：* 命中"未被任何显式范围碰到"的标签
curl -s -X POST http://localhost:8000/api/v1/negotiate-language \
  -H 'Content-Type: application/json' \
  -d '{"accept_language":"*,en;q=0","supported_languages":["en","fr","de"],"default_language":"en"}'
# -> selectedLanguage = "fr"（en 被 excluded_q0:exact 淘汰，fr/de 由 * 以 q=1 接受）

# 一切命中均被 q=0 排除 -> 回退默认语言
curl -s -X POST http://localhost:8000/api/v1/negotiate-language \
  -H 'Content-Type: application/json' \
  -d '{"accept_language":"*;q=0","supported_languages":["en","fr"],"default_language":"en"}'
# -> selectedLanguage = "en", usedFallback = true,
#    fallbackReason = "all_matches_excluded_q0", defaultExplicitlyExcluded = true
```

### 3) 无有效范围 → 回退默认语言

`accept_language` 为空串、纯空白或仅含空元素（如 `","`）时，视为客户端未表达偏好：

```jsonc
{ "accept_language": "", "supported_languages": ["en", "zh"], "default_language": "zh" }
// -> selectedLanguage = "zh", usedFallback = true, fallbackReason = "no_valid_ranges"
```

### 4) 非法语法 → 422

FastAPI/Pydantic 统一返回 `422 Unprocessable Entity`，错误定位到具体字段，例如：

```bash
curl -i -X POST http://localhost:8000/api/v1/negotiate-language \
  -H 'Content-Type: application/json' \
  -d '{"accept_language":"en;q=1.9","supported_languages":["en"],"default_language":"en"}'
# HTTP/1.1 422 ...  detail[0].msg = "Value error, invalid q-value: '1.9'"
```

以下情形同样返回 422：

- q 值非法（>1、负数、多于 3 位小数、`.5` 这类缺前导 0、引号包裹的 q）
- 语言范围非法（如 `englishish` 主子标签超 8 字母、`12-en` 数字主标签、`zh-*` 扩展通配符、`en-US-`）
- 参数语法错误（重复参数、空白位置非法、畸形键值对）
- `supported_languages` 为空、含非法 BCP 47 标签或重复
- `default_language` 非法或不在支持列表内
- 请求缺少必填字段 / 含未声明字段（`extra=forbid`）

---

## 协商规则（优先级语义）

对解析后的范围（范围比较大小写不敏感，`EN` 与 `en` 视为同一个）：

1. **q 权重优先**：能给候选带来更高 q 的范围决定候选得分；`q=0` 表示"不可接受"，
   被精确 / 前缀 / 通配符命中的标签立即排除，永不会入选。
2. **标签逐级截断（RFC 4647 Lookup）**：范围 `zh-Hant` 可命中 `zh-Hant-TW`
   （`prefix`）；同 q 下命中范围子标签越多（越具体）越优先，故 `en-US` 优先于 `en`。
   反向截断（较短服务端标签被较长范围命中，如 `fr-CH` 命中 `fr`）**不成立**。
3. **通配符 `*`**：只命中"没有被任何显式范围碰到"的标签；一旦某标签被显式范围
   命中（哪怕 `q=0`），`*` 即被遮蔽（响应证据中以 `shadowedByExplicit` 标记）。
4. **重复范围合并**：同一范围出现多次（如 `zh;q=0.7, ZH;q=0.95`）按
   "最高 q 胜出"合并，胜出条目保留其原始出现位置用于稳定排序；所有出现位置
   仍在 `parsedRanges[].occurrencePositions` 中可见（`mergePolicy: highest_q_wins`）。
5. **稳定决胜**：q 与特异性都相同的候选，按 `supported_languages` 中的先后顺序
   取靠前者（遍历时严格大于才替换），淘汰者轨迹带
   `lost_stable_tie_break:server_list_order`，并给出 `selectionRank`。
6. **回退**：无任何有效范围（`no_valid_ranges`）、或所有命中均被 `q=0` 排除
   （`all_matches_excluded_q0`）、或没有任何标签被范围命中
   （`no_acceptable_match`）时，选择 `default_language`，候选状态置为
   `fallback-selected`；若默认语言本身也被显式 `q=0` 排除，
   `defaultExplicitlyExcluded` 会置 `true`，调用方可据此决定是否返回 406。

> 说明：本服务是"解释器"——即便默认语言被 q=0 点名，仍按网关常见容错策略返回
> 默认标签，同时用 `defaultExplicitlyExcluded=true` 如实暴露该事实，由网关自行决定
> 是否改写为 406 Not Acceptable。

---

## 本地开发与测试（无需 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

uvicorn app.main:app --reload --port 8000      # 开发启动
pytest -q                                       # 运行测试（60 个用例）
```

测试覆盖：RFC 示例头、截断匹配、q=0 精确/前缀/通配排除、通配符遮蔽、重复范围
合并（大小写不敏感）、等权重服务端顺序稳定、空头部与无命中回退、BCP 47 校验、
以及 HTTP 层的 200/422 行为。

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI 路由：/health 与 /api/v1/negotiate-language
│   ├── negotiation.py   # 解析、合并、匹配、选择与轨迹（纯函数核心）
│   └── schemas.py       # Pydantic v2 请求/响应模型与 422 校验
├── tests/
│   └── test_negotiation.py
├── Dockerfile           # python:3.12-slim，非 root 用户，自带 HEALTHCHECK
├── docker-compose.yml   # 一条命令起服务，映射 8000 端口
├── requirements.txt
├── requirements-dev.txt
└── README.md
```
