# 桥梁结构监测与限行决策

融合传感、巡检、交通荷载和天气数据，生成限载限行或恢复建议。

监测批次、桥梁告警与交通通告接成可信结论链：同一桥梁的批次乱序到达时按有效时间窗合算，旧批次不能盖掉较新的重算结果；进入限行/封闭必须绑定有效交通通告，通告修改或批次作废后立即失效重算；读数写入中断后按批次号续写。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态、时间窗与基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、时间窗合算与通告绑定判定。
- `src/repository.py`：SQLite建表/迁移、事务、唯一约束、版本化结论和审计链。
- `src/service.py`：幂等登记、乱序合算、失效重算、权限检查和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则、失败场景、可信结论与HTTP并发测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8318
```

默认端口为`8318`，首次启动自动建库（旧库自动迁移通告绑定列）。使用`X-Actor`和`X-Role`请求头传递身份。

角色：`sensor_operator`（监测批次、读数、预警升级）、`bridge_engineer`（限行决策、批次作废）、`traffic_authority`（交通通告、封闭决策）、`viewer`。

## 可信结论规则

1. **首次登记幂等**：批次号全局唯一。两名操作员并发或断线重连提交同一批次，保留首次登记（含首次登记人），后续提交带`"replayed": true`返回同一份；读数分片按`(批次号, 分片序号)`同样幂等，重发返回首次写入的值。
2. **时间窗合算**：仅`finalized`、当前时刻落在`effective_from..effective_to`、且`observed_at`不晚于当前时刻的批次参与合算；窗内批次取最高告警等级、最大驱动偏差，结论的`basis_time`为窗内最新观测时间。
3. **单调结论**：结论逐版追加。非失效事件触发的重算若`basis_time`不晚于最新结论，只记`recompute_skip`审计，不覆盖较新结果（巡检单晚到不会让旧限行结论生效）。
4. **通告绑定**：进入`restricted`必须绑定`restriction`通告、进入`closed`必须绑定`closure`通告，且通告未作废、版本与绑定时一致、当前在通告时间窗内。
5. **立即失效**：通告修改（版本+1）、通告作废、通告时间窗到期（值班台`/live`读取即时感知）或批次作废，立即强制重算：限行/封闭自动降级到监测可支撑的状态并清除绑定；作废类事件即使新结论`basis_time`更旧也允许落账。
6. **断点续写**：批次头可声明`chunks_total`，未收齐分片禁止定稿；写入中断后凭批次号继续补片，定稿/作废重试返回同一份结果。

## 主要接口

桥梁告警：

- `GET /health`、`GET /api/items`、`POST /api/items`
- `GET /api/items/{id}`：当前已落账视图
- `GET /api/items/{id}/live`：值班台视图，读取时按当前时间窗即时重算
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`；进入限行/封闭须带`notice_id`
- `GET /api/items/{id}/batches`、`GET /api/items/{id}/conclusions`
- `GET /api/audit`

监测批次（`sensor_operator`）：

- `POST /api/items/{id}/batches`：登记批次头（`batch_no`、`effective_from/to`、`observed_at`、`severity`、可选`chunks_total`）
- `PUT /api/items/{id}/batches/{batch_no}/readings`（POST同义）：`chunk_index`+`quantity`，幂等续写
- `POST /api/items/{id}/batches/{batch_no}/finalize`：收齐后定稿并触发重算，重复提交幂等
- `POST /api/items/{id}/batches/{batch_no}/void`：批次作废并强制重算（`sensor_operator`/`bridge_engineer`）

交通通告（`traffic_authority`）：

- `GET /api/notices`、`POST /api/notices`（`notice_type`为`restriction`/`closure`）
- `PUT /api/notices/{id}`：修改通告时间窗或内容，版本+1，绑定告警立即失效重算
- `DELETE /api/notices/{id}`：作废通告，绑定告警立即失效重算

## 测试

```bash
python3 -m unittest discover -s tests -v
```
