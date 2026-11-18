"""V1/V3 共用实现（模型 / 归一化 / 存储 / AgentMoss 客户端 / analyze 服务）。

v1 与 v3 都从这里 import；v1 只保留薄 CLI 入口，v3 只保留 v3 专属能力
（mcp / gateway / proxy / pty / l3 / evidence 等）。结构对齐 TS 的 ts/src/shared。
"""
