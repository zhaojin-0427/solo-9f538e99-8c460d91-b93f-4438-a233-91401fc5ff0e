# 研究影像库 DICOM UID 隔离转换平台

多中心影像数据并入研究影像库时，按医院隔离重复 UID、保留跨批引用的 Web 服务。
基于 **Flask + pydicom + SQLite**，以 Docker Compose 一键启动。

## 功能与业务规则

| 主题 | 实现方式 |
| --- | --- |
| 输入 | 浏览器填写医院代号（2–20 位字母/数字/`_`/`-`），每批最多 10 份**未压缩 CT** DICOM |
| 接受的传输语法 | `1.2.840.10008.1.2` 隐式 VR LE、`1.2.840.10008.1.2.1` 显式 VR LE、`1.2.840.10008.1.2.2` 显式 VR BE；压缩语法或非 CT 整批拒绝 |
| UID 重写 | 检查（Study）、序列（Series）、实例（SOP Instance，含文件头 `MediaStorageSOPInstanceUID`）、参考坐标系（Frame of Reference）以及全部嵌套序列中的实体/引用 UID |
| 保持不变 | SOP 类 UID、传输语法 UID、实现/创建者/编码方案等类 UID（见 `app/uidmapper.py` 中 `NON_REMAPPED_UI_KEYWORDS`），以及 **PixelData 像素字节**（保存后重新读盘逐字节校验） |
| 新 UID 合法性 | `2.25.<128 位整数>`（DICOM 标准的 UUID 派生 UID，无需申请机构前缀），确定性派生：`UUIDv5(固定命名空间, 医院代号 + 原 UID)`，长度 ≤64、组件规则全部校验 |
| 跨批/重启不变 | 映射按 `(医院, 原 UID)` 持久化于 SQLite；同院同原 UID 永远得到同一新 UID，重启后依然成立 |
| 未入库引用 | 文件中出现但对应实体不在本批的引用 UID（如 `ReferencedSOPInstanceUID`、`ReferencedFrameOfReferenceUID`）同样计算并预留映射，后续批次自动收敛为同一值 |
| 医院隔离 | 医院代号参与派生输入，不同医院同一原 UID 得到不同新 UID；冲突判定也按医院分别进行 |
| 同字节重传 | 同院、同 SOP UID、同 SHA-256 重传 → 复用并返回原成品文件（仍登记新批次），不产生重复成品 |
| 异字节冲突 | 同院、同 SOP UID、不同字节 → **整批拒绝**（HTTP 302 + 页面错误提示），不写任何成品/映射 |
| 并发安全 | 进程内 `threading.RLock` 串行化发布 + SQLite `BEGIN IMMEDIATE` 写锁 + 映射唯一约束，并发上传不会出现两套映射 |
| 原子发布 | 成品先写 `staging/batches/<批次号>/`，单事务提交登记，再 `rename` 原子移动到 `published/`；失败回滚并清理暂存 |
| 崩溃恢复 | 启动时清空 `staging/batches/` 中未完成批次；只开放 `status='published'` 的完整批次，已发布成品重启后仍可下载 |
| 浏览器界面 | 上传表单、批次详情（旧→新 SOP 对照、逐文件下载）、按医院查看全部 UID 引用对应（含未入库预留映射） |

## 目录结构

```
app/
  __init__.py        # Flask 工厂、目录初始化、启动恢复
  config.py          # 路径/限制/允许的传输语法
  db.py              # SQLite schema 与连接管理
  uidmapper.py       # 确定性 UID 派生、合法性校验、改写/保留清单、分类
  dicom_engine.py    # pydicom 读取校验、UI 元素收集（含 file_meta/嵌套序列）、改写、像素校验
  services.py        # 批次编排：冲突检查、映射落库、暂存→原子发布、查询
  routes.py          # HTTP 路由
  templates/         # 上传页、批次页、映射对照页
tests/               # 31 项 unittest（单元 + 引擎 + 端到端 + 边界）
Dockerfile
docker-compose.yml
wsgi.py              # gunicorn 入口
```

## 启动

```bash
docker compose up --build
# 浏览器访问 http://localhost:8000
```

数据（SQLite、暂存区、已发布成品）保存在命名卷 `dicom-data`（容器内 `/data`），
重启容器后映射与成品均保留。

### 本地（无 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DICOM_DATA_DIR=./storage gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 8 wsgi:app
```

> 部署上固定 **1 个 gunicorn worker + 多线程**：进程锁在单进程内串行化批次发布，
> 读请求与下载仍并发；SQLite WAL 支持并发读、单写。

## 自测

```bash
python -m unittest discover -s tests -v
```

覆盖：UID 合法性/确定性/医院隔离、类 UID 与传输语法保留、像素字节不变、
隐式/显式/大端三种语法、非 CT 与压缩语法拒绝、多值 UI、同字节重传复用、
异字节整批冲突、跨批与重启映射稳定、医院互不混用、8 路并发无重复映射、
崩溃暂存清理、页面与下载 200/404。

## 关键设计说明

- **为什么派生中包含医院代号**：保证不同医院命名空间互不混用；同院内则由
  `(hospital_id, old_uid)` 唯一约束保证一对一，`new_uid` 全局唯一约束兜底碰撞。
- **为什么私有/未知 UI 标签不改写**：私有 UID 的语义无法从标准字典确认，
  仅改写 pydicom 字典中 VR=UI 且不在保留清单内的标准标签，避免破坏私有数据。
- **冲突检测先于任何写入**：所有文件先在请求工作区读盘建计划，确认无 SOP/字节
  冲突后才开启事务与暂存目录，因此冲突批次不会留下任何痕迹。
