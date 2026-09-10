"""WebUI 前端与后端。

注意：本模块**不**在包级别导入 ``app`` —— 否则 ``import tableseed.web``
会连带导入 fastapi，使 CLI 的惰性导入失去意义（NFR-5 / 既有项目约定）。
"""
