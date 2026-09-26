# 海上搜救协调系统

标准库实现的独立协调原型，使用 SQLite 保存事件、搜救资源、搜索区域、线索、离线批次和时间线。

## 运行

要求 Python 3.11+（在当前 Python 3.9 环境也可运行）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址为 `http://127.0.0.1:8206`，数据库默认为 `maritime_sar.db`。`--db`、`--host`、`--port` 可覆盖默认值。

## 主要接口

写操作使用 JSON，并需要 `X-User` 与 `X-Role` 请求头。角色包括 `coordinator`、`operator`、`field`、`analyst`、`viewer`。

- `GET /health`、`GET /api/state`
- `POST /api/incidents`：创建遇险事件并识别重复报警
- `POST /api/assets`：登记资源
- `POST /api/areas`：创建搜索区域
- `POST /api/assignments`：按能力、海况和航程分配资源
- `POST /api/clues`、`POST /api/clues/verify`
- `POST /api/assets/withdraw`：撤回资源并释放任务
- `POST /api/incidents/transfer`、`POST /api/incidents/close`
- `POST /api/offline/batch`：离线批次逐条回执合并
- `POST /api/offline/review`：对拒绝回执发起复查（`coordinator`/`analyst`）
- `GET /api/incidents/{id}/timeline`

## 离线回传回执

- 每条离线记录都有独立回执（`pending` 待处理 / `merged` 成功 / `rejected` 拒绝 + 原因），批次状态由回执汇总得出。
- 重传同一批次：已成功记录幂等返回、不重复写入；被拒记录用改正后的报文重试，可再合并。
- 事件结束后：迟到的记录只保留拒绝原因，不再写入或改动时间线。
- 复查可把拒绝记录置为待处理，也可附带改正内容直接合并；页面按批次展示待处理、成功、拒绝数量并可发起复查。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整协调流程、重复报警、错误位置、资源并发占用、离线逐条回执（幂等重传、改正再合并、结束后拒绝、复查）和权限拒绝。

## 局限

身份依赖调用方传入的用户和角色头；坐标使用球面距离近似；不会自动计算漂移概率区；文件附件、气象服务、真实通信链路和地理围栏未包含在内。
