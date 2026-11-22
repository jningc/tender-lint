# 归档

这两份不是错了，是**被后续文档吸收或替代**，留着会和现行架构打架。

| 文件 | 归档原因 | 去哪看现行版 |
|---|---|---|
| `检索改造方案.md` | 只讲到「检索内部怎么调」（分词、归一、RRF、重排），漏了更上一层的问题：六张表是 IE 不是 QA，主路径根本不该靠 top-k | `检索与扫描架构.md` 第 2–3 节；`架构（输出驱动）.md` 第二节 |
| `检索与扫描架构-审阅.md` | 审阅记录，列的问题都已修进 `检索与扫描架构.md` | 那份文档的正文 |

## 仍然有效的部分

**`检索改造方案.md` 里有两块内容没被吸收，将来做 `ask_box` 时要用：**

1. **「扩展」vs「归一」的对照**——`expand_query` 让 query 涨 3–5 倍、14/18 掉到 12/18；正确做法是索引与查询**双向归一**，query 长度不变。
2. **中文 BM25 默认做法**——jieba 词 + 字 bigram 混合，不是单字切；单字在短条款上区分度为零（「投标保证金」与「履约保证金」共享 保/证/金）。

这两块只对 `ask_box` 和 `_follow` 成立，不影响六张表的主路径。

## 参考

- [Anthropic Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval)：索引前加 context，失败率降 35% / 49% / 67%
- [ACORD (ACL 2025)](https://aclanthology.org/2025.acl-long.1206.pdf)：BM25 + GPT-4o 点式重排 NDCG@10 = 79.7，优于微调 cross-encoder 的 60.9
- [Azure AI Search RRF](https://learn.microsoft.com/en-us/azure/search/hybrid-search-ranking)：`1/(rank+60)`
