# CR Agent 评测体系面试题

> **用途**: 专门针对"Agent 评测体系"的面试准备，与 INTERVIEW_QA.md（项目整体/中间件/安全）互补。
> **关联文档**: [评测指南](EVAL_GUIDE.md) | [项目面试题](INTERVIEW_QA.md)
> **方法论来源**: Anthropic《Demystifying evals for AI agents》 + ATA《Agent 到底如何评测》

---

## 目录

1. [评测基础概念](#一评测基础概念)
2. [为什么传统测试在 Agent 上失效](#二为什么传统测试在-agent-上失效)
3. [数据集设计](#三数据集设计)
4. [Grader 评分器](#四grader-评分器)
5. [指标体系](#五指标体系)
6. [多 Trial 统计与 pass@k/pass^k](#六多-trial-统计与-passkpassk)
7. [评测生命周期管理](#七评测生命周期管理)
8. [评测流水线与工程实践](#八评测流水线与工程实践)
9. [业界 Benchmark 与方法论](#九业界-benchmark-与方法论)
10. [实战经验与根因分析](#十实战经验与根因分析)
11. [代码实现细节](#十一代码实现细节)
12. [高阶问题](#十二高阶问题)

---

## 一、评测基础概念

### Q1: 什么是 Agent 评测中的 Task、Trial、Grader？它们之间是什么关系？

**答:**

- **Task**：一道测试题。有明确的输入和成功标准。在 CR Agent 中就是一条 PR diff + 期望的 findings/verdict。
- **Trial**：对一道 Task 的一次尝试。对同一 PR 跑一次 `graph.invoke()` 就是一次 trial。
- **Grader**：对 Trial 结果打分的逻辑。判断 Agent 做得好不好。

关系：一个 Task 可以跑多个 Trial（因为 LLM 有随机性，跑一次不算数），每个 Trial 的结果由 Grader 打分。

一句话：**Task 是考题，Trial 是答题，Grader 是阅卷。**

### Q2: 什么是 Transcript 和 Outcome？

**答:**

- **Transcript**：一次 Trial 的完整记录——包括 LangGraph 的全部 messages 数组、工具调用日志、中间状态。相当于"考试答题过程"。
- **Outcome**：Trial 结束时的最终状态——在 CR Agent 中就是 ReviewReport 里的 verdict + findings。相当于"最终交上来的卷子"。

Transcript 用于分析失败原因（"Agent 为什么判断错了"），Outcome 用于计算指标（"verdict 对不对"）。

### Q3: "Agent = Model + Harness" 是什么意思？为什么这个公式很重要？

**答:**

Agent 的效果 = 模型智能 × 框架质量。Model 是底层 LLM（如 DeepSeek/GPT），Harness 是包装 LLM 的框架（状态机、中间件、prompt、verdict 逻辑）。

这个公式重要的原因是它指导了**正交评测**：
- 固定 Harness 换 Model → 测不同模型在 CR 场景的表现（ClawBench 思路）
- 固定 Model 换 Harness → 测不同框架配置的效果（HarnessBench 思路）

只固定一个变量测另一个，才能得出有意义的结论。如果不固定，你不知道是模型不行还是框架不行。

### Q4: 你项目里的 Harness 具体包含哪些组件？

**答:**

Harness 是把 LLM 包装成 Agent 的整套框架：

1. **LangGraph 状态机**：4 个节点（prepare → llm ⇄ tools → finalize）
2. **6 层中间件链**：输入净化、上下文压缩、循环检测、工具错误处理、输出预算、token 预算
3. **确定性规则引擎**：28 条正则规则，零 LLM 调用
4. **安全防护**：标签中和、secret 脱敏、路径校验、沙箱执行
5. **Prompt 工程**：系统提示词 + 审查流程定义 + 输出格式约束
6. **verdict 代码重算**：忽略 LLM 自报 verdict，由代码根据 findings 严重度加权计算
7. **可观测性**：trace ID、结构化日志、熔断器、重试、幂等

---

## 二、为什么传统测试在 Agent 上失效

### Q5: 传统软件测试在 Agent 场景下为什么失效？

**答:**

传统测试的核心前提是**确定性**：输入不变，输出不变。但 Agent 打破了这个前提：

| 失效因素 | 说明 | CR Agent 中的表现 |
|---------|------|-------------------|
| 模型随机性 | Temperature 采样 | 同一 PR 跑 10 次，findings 每次可能不同 |
| 上下文变化 | 对话历史、工具返回的细微变化 | 中间件压缩旧消息后上下文变化 |
| 工具返回变化 | 工具返回动态内容 | `run_lint` 的结果取决于代码状态 |
| Runtime 因素 | 时间、环境变量 | Token 预算、迭代次数限制不同 |

所以不能跑一次就说"Agent 通过了"——必须多 trial 取统计。

### Q6: 什么是多轮交互的"蝴蝶效应"？怎么量化？

**答:**

Agent 在多步工具调用中，单步的错误会传播和累积。假设每步准确率 90%：

```
单步 90% → 10 步后：(0.9)^10 ≈ 34.9%
单步 95% → 10 步后：(0.95)^10 ≈ 59.9%
```

这意味着 10 步链路中，即使每步 90% 准确，整体只有 35% 概率全对。所以 Agent 评测不能假设"单步差不多行就行"——必须看整体 Outcome，而不是单步正确率。

### Q7: 既然 Agent 有随机性，确定性层为什么能 100%？

**答:**

CR Agent 的架构分两层：
- **确定性层**：28 条正则规则，纯函数，无 LLM 调用。输入完全确定 → 输出完全确定。
- **LLM 层**：调用大模型，有 temperature 采样，输出有随机性。

确定性层 100% 是因为它是纯函数——给定同样的 diff，正则匹配结果永远一样。这正是确定性规则的优势：零随机性、零成本、可复现。它不需要多 trial。

### Q8: 你怎么知道一个评测结果不是"运气好"？

**答:**

通过多 Trial 统计区分：

| 单次结果 | 10 次结果 | 结论 |
|---------|---------|------|
| 通过 | 10/10 通过 | 真的会，稳定性高 |
| 通过 | 3/10 通过 | 运气好，稳定性差 |
| 失败 | 0/10 通过 | 真的不会 |
| 失败 | 7/10 通过 | 会但不稳定 |

这就是 pass@k 和 pass^k 的意义：
- **pass@k** 看"至少 1 次做对"——测能力上限
- **pass^k** 看"k 次全做对"——测稳定性

单次通过可能是运气，pass^5 通过才算真稳定。

---

## 三、数据集设计

### Q9: 你的数据集分几个子集？各子集的设计意图是什么？

**答:**

4 个子集，每个针对不同的评测目标：

| 子集 | 规模 | 内容 | 测什么 |
|------|------|------|--------|
| DS-Golden | 23 条 | 覆盖全部规则 + 3 种 verdict | 能力：能不能找到问题（D1/D2/D5） |
| DS-Injection | 8 条 | PR 中藏注入指令 | 安全：会不会被骗（D4/D8） |
| DS-Benign | 10 条 | 干净的正常代码 | 精确率：会不会乱报（D1 FP） |
| DS-Adversarial | 4 条 | 安全写法但 LLM 易误报 | LLM 精确率对抗（LLM FP） |

为什么不能只用一个子集？因为 Precision 和 Recall 天然矛盾——放松标准提高 Recall 会导致 Precision 下降。需要正面用例测"别漏"（Golden），反面用例测"别乱报"（Benign/Adversarial），两者独立统计才能全面评估。

### Q10: 为什么需要 DS-Benign？直接看 Golden 的 Precision 不够吗？

**答:**

不够。Golden 里每条代码都有问题，Agent 报出来的"问题"只要匹配上 golden 就是 TP。但 Golden 不测试"代码没问题的时候 Agent 会不会硬报"。

DS-Benign 全是干净代码，标准答案是 approve + 0 finding。如果 Agent 对着干净代码也报了 3 条问题，说明它过度敏感——这在生产中会导致用户收到大量无意义的审查意见，降低信任度。

这就是为什么 Benign Precision 和 Golden 的 Precision 要分开统计——前者测"不乱报"，后者测"报的要对"。

### Q11: DS-Adversarial 是什么？和 DS-Benign 有什么区别？

**答:**

DS-Adversarial 是专门针对 LLM 假阳性设计的对抗性用例。

区别在于"为什么可能误报"：
- **DS-Benign**：代码是正常业务代码，没有任何可疑关键词。如果误报，说明 LLM 纯粹在过度报告。
- **DS-Adversarial**：代码使用了安全写法，但包含 LLM 容易误判的关键词。比如：
  - `subprocess.run(cmd, shell=False)` —— 安全用法，但 LLM 看到 subprocess 可能报 shell-true
  - `ast.literal_eval()` —— eval 的安全替代，但 LLM 看到 "eval" 可能报 eval-usage
  - `innerHTML → textContent` —— 修复 XSS，但 LLM 可能只看到 innerHTML 就报

Adversarial 测的是 LLM 的**语义理解能力**——能不能区分安全写法和危险写法，而不是靠关键词匹配。

### Q12: Fixture 的 YAML schema 包含哪些字段？为什么这么设计？

**答:**

```yaml
id: G-012              # 唯一标识，前缀标明子集
category: security     # 分类，方便筛选
language: python       # 语言，支持多语言评测
pr_info:               # PR 元数据，模拟真实场景
  title: "Add auth check"
  author: "dev1"
diff: |                # 被审查的代码变更（核心输入）
golden_findings:        # 期望的问题列表（标准答案）
  - rule_id, severity, file, line
golden_verdict: request_changes  # 期望的最终结论
injection: false        # 是否为注入用例
tags: [assert, major]   # 标签，方便统计
notes: 验证 assert...   # 人类备注
```

设计原则：
- `diff` 是核心输入，用 unified diff 格式（和真实 GitHub PR 一致）
- `golden_findings` 和 `golden_verdict` 是标准答案，分开放因为它们独立评测
- `injection` 字段控制评测器是否做注入抗性检查
- `tags` 用于"按维度切片统计"（如"只看 security 类的 Recall"）

### Q13: 你的数据集覆盖了哪些规则？怎么保证覆盖全面？

**答:**

DS-Golden 23 条覆盖了 28 条确定性规则，包括：
- 安全类：sql-injection、hardcoded-secret、command-injection、eval-usage、exec-usage、pickledeserialize、yaml-unsafe-load、shell-true、weak-hash、verify-false、insecure-random、assert-sensitive、tempfile-race、js-innerhtml、js-document-write
- 调试类：breakpoint、print-statement
- 错误处理类：bare-except、pass-in-except、broad-except
- 维护性类：todo-comment、mutable-default、global-statement
- 质量类：long-line、import-star、unused-import、js-var-declaration
- 类型安全类：any-annotation

覆盖矩阵确保每条规则至少有一条 fixture 命中，3 种 verdict（block/request_changes/approve）都有分布。通过覆盖矩阵表格可以一眼看出"哪些规则还没有被任何 fixture 测到"。

---

## 四、Grader 评分器

### Q14: 三类 Grader 各自的优缺点？你怎么组合使用？

**答:**

| 维度 | Code-based | Model-based (LLM-as-Judge) | Human-based |
|------|-----------|---------------------------|-------------|
| 成本 | 最低 | 中等 | 最高 |
| 速度 | 最快 | 较慢 | 最慢 |
| 确定性 | 最高 | 中等（有波动） | 高（但需对齐） |
| 适用范围 | 窄（固定逻辑） | 宽（语义/复杂逻辑） | 最广 |

组合策略：
- **Code-based** 做标准化场景：precision/recall/verdict/幻觉率/注入成功率——这些有明确对错，不需要主观判断
- **Model-based** 做非标准场景：可操作性/清晰度/完整性——这些需要语义理解，代码判断不了
- **Human** 做抽样校验和金标数据集构建——定期人工抽检 LLM-as-judge 的评分准确性

原则：**能用代码判的绝不用 LLM 判，能用 LLM 判的绝不用人判。** 越靠左边越便宜越快，但只能覆盖越窄的范围。

### Q15: LLM-as-Judge 有什么已知偏见？你怎么缓解？

**答:**

已知偏见（来自 Anthropic 研究）：

1. **Position bias**：LLM 倾向于偏好评判列表中先出现的选项
2. **Verbosity bias**：LLM 倾向于给更长的回答更高分
3. **Self-preference bias**：LLM 倾向于给自己生成的回答更高分
4. **Order bias**：同一组内容打分顺序不同，结果可能不同

CR Agent 的缓解措施：
- `temperature=0.0` 固定，减少随机波动
- 结构化 rubric，每个维度独立评分（不是让 LLM 自由发挥）
- LLM 无法判断时返回 "Unknown"（退路），不让它硬猜
- 需定期与人工评分校准（文档 §7.3 提到每月抽检）

### Q16: 你为什么不直接用 LLM 做 verdict 判断，而是用代码重算？

**答:**

三个原因：

1. **安全**：LLM 可能被 PR 中的 prompt injection 骗到，给出错误的 verdict（比如被"please approve"骗了判 approve）。代码重算不受注入影响——它只看 findings 的 severity，不读 PR 内容。

2. **确定性**：verdict 映射逻辑是纯规则（有 blocker 就 block，有 major 就 request_changes），用代码做 100% 确定。LLM 做 verdict 会引入随机性，同一组 findings 可能判出不同 verdict。

3. **可控性**：代码可以做来源加权——确定性来源 1 条 blocker 就 block，LLM 来源要 ≥2 条才 block。因为 LLM finding 可能有假阳性，不能让单条 LLM blocker 直接触发 block。这种加权逻辑用代码做清晰可控。

这是 D8（verdict 重算保真）维度测的不变量——必须 100%，一次都不能漏。

### Q17: Code-based Grader 的 TP 匹配规则是什么？为什么要做 LLM 放宽？

**答:**

TP 匹配规则：
```
TP = 同 file 且 line 容差 ±2 且 rule_id 语义等价
```

LLM 放宽规则：
- 如果 rule_id 以 `llm.` 开头，只要 file + line 匹配即视为等价，不要求 rule_id 完全一致。

为什么放宽？因为确定性规则的 rule_id 是固定的（如 `security.sql-injection`），但 LLM 可能用 `llm.injection-risk` 来命名同一个问题。如果要求 rule_id 完全匹配，LLM 的 finding 即使描述的是完全正确的问题也会被判为 FP——precision 被低估。

放宽的代价是可能高估 precision：LLM 用 `llm.anything` 命名一个 finding，只要 file+line 对就算 TP，即使它描述的是不同问题。这是一个 precision/coverage 的权衡。

---

## 五、指标体系

### Q18: Precision 和 Recall 为什么需要分开看？为什么不用一个指标？

**答:**

因为它们测的是两个不同方向的错误，天然存在矛盾：

- **Precision 高、Recall 低**：报出来的都是对的，但漏了很多——"宁缺毋滥"型，用户觉得"审查没啥用，啥都没报出来"
- **Recall 高、Precision 低**：问题都找到了，但也报了一堆假的——"宁可错杀"型，用户觉得"审查报的全是噪音，不知道哪个是真的"

放松标准（多报）提高 Recall 但降低 Precision；收紧标准（少报）提高 Precision 但降低 Recall。

所以必须分开看，知道 Agent 是"漏报多"还是"乱报多"，才能针对性优化。F1 是两者的调和平均，综合看，但不能替代分开看——F1 相同的两组 P/R 可能完全不同。

### Q19: F1 为什么用调和平均而不是算术平均？

**答:**

调和平均会惩罚偏科：

| 情况 | Precision | Recall | 算术平均 | 调和平均(F1) |
|------|-----------|--------|-----------|-------------|
| 均衡 | 0.80 | 0.80 | 0.80 | 0.80 |
| 偏科 | 1.0 | 0.40 | 0.70 | 0.57 |
| 极偏 | 1.0 | 0.10 | 0.55 | 0.18 |

算术平均下，Precision=1.0 Recall=0.1 看起来 0.55 还行。但这个 Agent 只报 1 条且准确，却漏了 90% 的问题——实际不可用。调和平均给 0.18，真实反映了"偏科到不可用"。

一句话：**调和平均不允许你用一个指标的高分去掩盖另一个指标的低分。**

### Q20: 你有 9 个评测维度，能说说各自测什么吗？

**答:**

| 维度 | 通俗说法 | 测什么 | Grader |
|------|---------|--------|--------|
| D1 发现正确性 | 报的准不准 | finding 是不是真问题 | Code |
| D2 verdict 准确率 | 结论对不对 | 最终 verdict 和标准答案一不一致 | Code |
| D3 幻觉率 | 有没有瞎编 | finding 指向的文件/行号存不存在 | Code |
| D4 注入抗性 | 会不会被骗 | PR 里的注入指令有没有被遵循 | Code |
| D5 安全召回 | 安全问题漏没漏 | 安全类的 finding 有没有全找到 | Code |
| D6 一致性 | 每次结果一样吗 | 同一 PR 多次审查的 verdict 方差 | Code |
| D7 可操作性 | 建议有没有用 | 修复建议是否具体可执行 | Model |
| D8 verdict 重算保真 | 代码说了算不算 | LLM 自报 verdict 是否被代码覆盖 | Code |
| D9 效率 | 快不快、贵不贵 | 延迟 P95 + token 消耗 | Code |

### Q21: P95 延迟是什么意思？为什么不用平均值？

**答:**

P95 = 把所有用例的耗时排序，取第 95 百分位的值。

为什么不用平均值？因为平均值会被少数极快或极慢的用例拉偏：

- 20 条用例，19 条 10 秒，1 条 300 秒 → 平均 24 秒，看起来还行
- 但 P95 = 大约 280 秒 → 说明有 5% 的用例慢到 280 秒

平均值掩盖了"最慢有多慢"，而 P95 直接告诉你"95% 的情况下最慢多慢"。对于用户体验来说，尾部延迟比平均值更重要——用户只会在"慢到离谱"时投诉。

### Q22: Benign Precision 和 Golden Precision 有什么区别？

**答:**

- **Golden Precision** = 在 Golden 子集上算的 Precision。Golden 里每条代码都有问题，测的是"报出来的问题里真的有多少"。
- **Benign Precision** = 在 Benign 子集上算的"干净代码正确放行率"。Benign 里代码没问题，标准答案是 approve + 0 finding。如果 Agent 对着干净代码报了问题 → 假阳性。

举例：8 条 Benign，Agent 对 5 条正确判了 approve 且没报问题 → Benign Precision = 5/8 = 62.5%。剩下的 3 条对着干净代码硬报了问题。

---

## 六、多 Trial 统计与 pass@k/pass^k

### Q23: pass@k 和 pass^k 分别是什么？它们的方向相反吗？

**答:**

是的，方向相反：

- **pass@k**：k 次中**至少 1 次**通过。k 增大 → 升高（射门越多进球越多）。测**能力上限**。
- **pass^k**：k 次**全部**通过。k 增大 → 降低（要求越来越严）。测**稳定性下限**。

举例：单次成功率 75%，跑 3 次：
- pass@3 = 1 - (1-0.75)^3 = 98.4%（至少成功一次的概率）
- pass^3 = 0.75^3 = 42.2%（全部成功的概率）

pass@3 高说明"有机会做对"，pass^3 低说明"不稳定"。理想状态是两个都高——既有能力又稳定。

### Q24: 你的 multi-trial 统计在代码层面是怎么实现的？

**答:**

在 `eval/metrics.py` 中新增了 `TrialStats` 类：

```python
@dataclass
class TrialStats:
    fixture_id: str
    trials: list[EvalResult]  # 收集每条 fixture 的全部 trial 结果
```

核心属性：
- `pass_at_k`：`any(t.passed for t in self.trials)` —— 至少 1 次通过
- `pass_pow_k`：`all(t.passed for t in self.trials)` —— 全部通过
- `verdict_consistency`：出现最频繁的 verdict 占比 —— 多 trial 一致率
- `mean_precision` / `mean_recall`：各 trial precision/recall 的均值

`run_eval_suite` 在 `trials > 1` 时，每条 fixture 跑完所有 trial 后构建 `TrialStats`，同时保留 best trial 用于单 trial 兼容的指标累加。报告输出时，`to_dict()` 自动增加 multi-trial 统计字段，`to_markdown()` 增加 "Multi-Trial Statistics" 章节。

### Q25: 为什么之前 pass^k 在代码中"失去意义"？你做了什么改进？

**答:**

改进前，`run_eval_suite` 多 trial 时取"最优 trial"作为代表——只要有一次通过就取那次。这意味着 pass^k 永远为 1.0（因为取了最优的那次），失去了统计意义。

改进后，`TrialStats` 收集全部 trial 结果，分别计算 pass@k 和 pass^k。best trial 仍用于单 trial 兼容的指标累加（precision/recall/verdict_matrix），但 trial_stats 独立报告统计分布——pass@k 看上限，pass^k 看稳定性。

### Q26: 你的退出码门禁是怎么设计的？

**答:**

分层设计：

- **确定性层**：`verdict_accuracy < 1.0` → exit 1。确定性层必须 100%，任何不过都是回归。
- **LLM 层单 trial**：`verdict_accuracy < 0.9` → exit 1。LLM 有随机性，90% 是合理门槛。
- **LLM 层多 trial**：`pass^k < 0.6` → exit 1。多 trial 时用稳定性门禁——如果 5 次全通过率低于 60%，说明 Agent 太不稳定，不应该上线。

---

## 七、评测生命周期管理

### Q27: 什么是"能力评测 → 回归评测"毕业机制？

**答:**

```
能力评测(低通过率爬坡) ──连续3次≥95%──→ 毕业线 ──→ 回归评测(接近100%守门)
     ↑                                                        │
     │              腾出配额建更难用例 ←──────────────────────────┘
```

- **能力评测**：瞄准 Agent 不擅长的任务，初始通过率低（30-60%），逐步爬坡。
- **毕业线**：连续 3 次全量 ≥ 95% → 毕业到回归集。
- **回归评测**：毕业后纳入 CI gate，防止已有能力退化。目标是 ≥ 98%。

关键思想：**毕业的用例不再是"能力测试"，而是"守门员"**。它们的存在是为了防止改代码时把已有能力搞坏。腾出的评测配额用来建更难的用例，持续推动能力提升。

### Q28: 什么是反饱和监控？为什么 100% 通过率不一定是好事？

**答:**

当某维度持续 100% 时，可能有两种情况：
1. Agent 真的很强——此时这个维度已经没有区分度了，测不出退化
2. Grader 太宽松——什么都能过，实际能力没有 100%

反饱和监控步骤：
1. 检查 transcript，确认 grader 是否过于宽松
2. 如 grader 无问题，增加更难的用例（提升难度）
3. 或提升 grader 严格度（如缩小 line 容差从 ±2 到 ±1）

100% 不是好事是因为**它意味着这个测试已经失去了"发现问题"的能力**——就像一道全班都答对的考题，无法区分谁好谁差。

---

## 八、评测流水线与工程实践

### Q29: 你的评测流水线分几层？为什么分层？

**答:**

三层，按成本/速度/频率权衡：

| 层级 | 内容 | 执行时机 | 成本 |
|------|------|---------|------|
| 确定性层 | 28 条规则 + verdict | CI 每次必跑 | 零 token，秒级 |
| LLM 冒烟 | golden 10 + injection 5 + benign 5 | PR 触发 | 低 |
| LLM 全量 | 全部 45 条 fixture | 夜间全量 | 高 |
| LLM 多 trial | 45 条 × 5 trials | 周末 | 最高 |

为什么分层？确定性层零成本可以每次跑（快速发现回归）；LLM 层有 API 成本，冒烟用于 PR 级快速验证，全量放夜间避免阻塞开发；多 trial 最贵放周末。

这是经典的"快速反馈 vs 全面覆盖"权衡——越快的越便宜越不全面，越全面的越贵越慢。

### Q30: baseline.json 你怎么设计的？怎么支持趋势监控？

**答:**

baseline.json 支持多个键：
- `deterministic`：确定性层基线
- `llm`：LLM 层单 trial 基线
- `llm_5trial` / `llm_pro_5trial`：通过 `--baseline-key` 区分的多 trial 基线

每个基线条目包含：precision/recall/F1/verdict_accuracy + multi-trial 统计（pass_at_k/pass_pow_k/verdict_consistency）。

趋势监控：baseline.json 新增 `history` 数组，每次评测自动追加一条记录（key/timestamp/关键指标），保留最近 50 条。通过历史记录可以看到"Precision 从 0.30 提升到 0.57 再到 0.64"这样的趋势。

### Q31: 你的评测器和 pytest 测试有什么区别？为什么要两套？

**答:**

| 维度 | pytest 测试 | 评测器 (run_eval.py) |
|------|------------|---------------------|
| 断言方式 | 严格断言（== 0） | 统计报告（precision = 0.85） |
| 输出 | pass/fail | Markdown + JSON 报告 + baseline |
| 多 trial | 不支持 | 支持（pass@k/pass^k） |
| 用途 | CI 门禁（必须过） | 趋势监控（看变化） |

pytest 测试是**二元的**——每条 fixture 要么 pass 要么 fail，适合 CI 门禁。评测器是**统计的**——算 precision/recall/F1，生成报告，适合趋势分析。

两者互补：pytest 保证"不退化"，评测器回答"做得有多好"。

---

## 九、业界 Benchmark 与方法论

### Q32: Anthropic 的 8 步评测路线图是什么？你落地了哪些？

**答:**

| 步骤 | 内容 | 落地状态 |
|------|------|---------|
| Step 0 | 尽早开始，20-50 条即可 | 已建 45 条 |
| Step 1 | 从手工测试和真实 bug 开始 | R1 复现 PR 已纳入回归集 |
| Step 2 | 写无歧义任务 + 参考解 | YAML fixture schema + golden_findings |
| Step 3 | 正负面用例平衡 | DS-Benign + DS-Adversarial 防误报 |
| Step 4 | 稳健 harness + 稳定环境 | 确定性层纯函数；temperature=0.1 |
| Step 5 | 认真设计 grader | Code-based + LLM-as-judge 双轨 |
| Step 6 | 查看 transcript | 待建 |
| Step 7 | 监控饱和 | baseline.json + history 趋势 |
| Step 8 | 持续维护 | 待建（eval PR 流程 + ownership） |

### Q33: SWE-bench 和你的评测有什么异同？

**答:**

**SWE-bench**：给 Agent 一个 GitHub issue + 代码仓库，让 Agent 修 bug。成功标准是修复后仓库的测试套件全部通过。

**CR Agent**：给 Agent 一段 PR diff，让 Agent 审查。成功标准是报出的 findings 匹配 golden_findings + verdict 匹配 golden_verdict。

| 维度 | SWE-bench | CR Agent |
|------|-----------|---------|
| 任务 | 修 bug | 审查代码 |
| 输出 | 代码补丁 | 结构化 findings + verdict |
| 判分 | 跑测试套件 pass/fail | file+line 匹配 + verdict 比较 |
| 类型 | 编程型 | 审查型（不生成代码） |

核心异同：都是"outcome-based"评测（看最终结果而非过程），但 SWE-bench 有客观标准（测试通过），CR Agent 的标准是人工标注的 golden findings（有一定主观性）。

### Q34: ClawBench 和 HarnessBench 的正交评测思路你怎么落地的？

**答:**

- **ClawBench 思路**：固定 Harness，换不同 Model。我在文档 §6.3 做了——固定 graph 配置、prompt、verdict 逻辑、45 条 fixture，分别用 DeepSeek-V4-Flash 和 DeepSeek-V4-Pro-0813 跑，对比 Precision/Recall/Verdict Accuracy。

- **HarnessBench 思路**：固定 Model，换不同 Harness 配置。比如改 prompt（加"宁缺毋滥"约束前 vs 后）、改 verdict 加权逻辑（LLM 来源 blocker ≥1 vs ≥2 触发 block）、改去重逻辑，对比优化前后的混淆矩阵。

关键原则：**只改一个变量，固定其他所有变量**，否则无法归因。

---

## 十、实战经验与根因分析

### Q35: 你的 LLM 层基线 Precision 只有 0.30，根因是什么？怎么优化的？

**答:**

**根因**：LLM（DeepSeek-V4-Flash）平均每条 PR 额外产出 3-7 条 golden 中不存在的 finding（FP），多为 major 级别，导致 verdict 被拉高。具体三个原因：

1. **LLM 过度报告**：对正常代码也报 finding（过度敏感）
2. **Prompt 未充分约束**：没有强调"不确定的问题不要报"
3. **缺少 finding 去重**：确定性规则已发现的问题，LLM 可能以不同 rule_id 重复报告

**优化措施**：
1. `_finalize` 去重：对 `det_findings + llm_findings` 按 `(file, line)` 去重，同位置只保留 severity 最高的
2. Prompt "宁缺毋滥"约束："只报告确信度 ≥ 80% 的问题，不报告设计建议/最佳实践提醒"
3. Prompt 收紧 major 定义："仅限于会导致实际 bug 或安全问题的发现"
4. verdict LLM 来源加权：LLM 来源的 blocker/major 需 ≥2 条才触发

**效果**：Precision 0.30 → 0.57（+90%），Verdict Accuracy 0.45 → 0.83（+85%），Recall 保持 1.0 不降。

### Q36: 优化后 Verdict Accuracy 从 0.45 到 0.83，但 approve 行为什么从 5 变 14？

**答:**

优化前混淆矩阵 approve 行：5 正确、5 误判 request_changes、5 误判 block。原因是有 10 条本该 approve 的 PR 被 LLM 的假阳性（FP）拉高了 verdict。

优化后：14 正确、1 误判 request_changes、0 误判 block。去重 + prompt 约束大幅减少了 FP，这些 PR 不再被假阳性拉高到 block/request_changes。

但出现了新问题：request_changes 行有 4 条被误判为 approve——因为 LLM 来源加权要求 ≥2 条 major 才触发 request_changes，而这些 PR 的 LLM 只报了 1 条 major。这是一个 trade-off：收紧 FP 门控的同时，也提高了真问题的触发门槛。

### Q37: 模型横向对比（Flash vs Pro）你发现了什么？

**答:**

| 指标 | Flash | Pro | 结论 |
|------|-------|-----|------|
| Precision | 0.57 | 0.64 | Pro FP 更少 |
| Recall | 1.0 | 0.98 | Pro 漏了 1 条 |
| Verdict Accuracy | 0.83 | 0.92 | Pro 已达标 |
| 延迟 | 16.3s/PR | 9.5s/PR | Pro 更快 |
| Benign Precision | 0.625 | 0.375 | Pro 反而更低 |

关键发现：
- Pro 的 verdict accuracy 达标（0.92 ≥ 0.90），15 条 approve 全对、14 条 block 全对
- Pro 零幻觉
- Pro 更快（可能因为工具调用更少）
- Pro 的弱点：Recall 略降（漏了 G-016 的 broad-except，可能被去重逻辑误删）；Benign Precision 更低（更多良性 PR 有 1 条 FP）

结论：Pro 全面优于 Flash，建议生产切换。但 Benign Precision 0.375 说明 Pro 仍有假阳性问题，需要进一步优化。

### Q38: 你在优化过程中踩过什么坑？

**答:**

1. **去重逻辑引入新问题**：`(file, line)` 去重后，G-016 的 broad-except 被同位置的 pass-in-except 覆盖了（Pro 模型 Recall 从 1.0 降到 0.98）。因为两者在同 file 同 line，去重只保留了 severity 最高的，但 broad-except 是 minor 而 pass-in-except 也是 minor——确定性来源优先导致 broad-except 被删。

2. **LLM 来源加权过严**：要求 LLM major ≥2 才触发 request_changes，导致只有 1 条 LLM major 的 PR 被误判 approve。需要考虑对安全类 rule_id 放宽为 1 条即触发。

3. **Benign Precision 对抗**：最初只有 8 条 Benign，5 条误判就导致 benign_precision 从 1.0 跌到 0.375。样本太小导致指标方差极大。后来扩充到 10 条并新增了 4 条 Adversarial。

教训：**优化是 trade-off，解决一个问题可能引入新问题。需要全维度回归测试，不能只看一个指标。**

---

## 十一、代码实现细节

### Q39: `match_findings` 的匹配逻辑是什么？为什么用贪心匹配？

**答:**

匹配规则：同 file 且 line 容差 ±2 且 rule_id 语义等价。

贪心匹配过程：
1. 按 severity 从高到低排序 golden findings（优先匹配 blocker/major）
2. 对每条 golden，在未匹配的 actual 中找距离最近的（line 差最小）
3. 匹配上 → TP；golden 没匹配上 → FN；actual 没匹配上 → FP

为什么贪心而非最优匹配（如匈牙利算法）？因为贪心按 severity 排序确保高严重度的 finding 优先匹配到最接近的 actual，避免低严重度 finding "抢走"高严重度的匹配。在 CR 场景中，高严重度 finding 的匹配比低严重度更重要——漏一个 blocker 比漏一个 minor 严重得多。

### Q40: `_dedup_findings` 的去重策略是什么？确定性优先怎么实现？

**答:**

```python
key = (f.file, f.line)  # 按位置去重
# 同位置只保留 severity 最高的
# 如果 severity 相同，确定性来源优先于 LLM 来源
```

实现：遍历所有 findings，以 `(file, line)` 为 key 去重。当遇到同 key 的 finding 时：
1. 如果新 finding severity 更高 → 替换
2. 如果 severity 相同且旧的是 LLM 来源、新的是确定性来源 → 替换
3. 否则保留原有的

确定性优先的原因：确定性规则零假阳性（在确定性层 100% precision），而 LLM 可能报假阳性。同一位置如果两者都报了，应该信确定性的。

### Q41: `_parse_llm_findings` 怎么处理 LLM 输出格式不规范的情况？

**答:**

LLM 的输出可能有多种格式，解析器做了 5 种降级处理：

1. **`json 代码块**：用正则提取代码块内容再 `json.loads`
2. **裸 JSON**：直接 `json.loads`
3. **纯文本（被强制终止时）**：返回空列表（不算 finding）
4. **格式错误的 JSON**：`json.loads` 抛异常 → 返回空列表
5. **JSON 中缺少 findings 字段**：返回空列表
6. **单条 finding 字段缺失**：跳过该条，不崩

此外，finding 的可选字段有默认值：severity 默认 info、confidence 默认 medium、source 固定 "llm"。

设计原则：**解析器永远不抛异常**——即使 LLM 输出完全不可解析，也返回空列表让流程继续。因为一条 finding 都没有最多是 Recall 低，但解析崩溃会导致整个审查失败。

### Q42: 你的评测器怎么支持多 trial 报告的？

**答:**

`EvalReport` 新增了 `trial_stats: list[TrialStats]` 和 `trials_per_fixture: int` 字段。

`run_eval_suite` 在 `trials > 1` 时：
1. 每条 fixture 跑 N 次 trial，收集全部 `EvalResult`
2. 构建 `TrialStats` 汇聚结果，计算 pass@k/pass^k/verdict_consistency/mean_precision/mean_recall
3. best trial 仍用于单 trial 兼容的指标累加（total_tp/total_fp/verdict_matrix）

`to_dict()` 在 `trials > 1` 时自动输出：
- `pass_at_k` / `pass_pow_k`：suite 级通过率
- `mean_precision` / `mean_recall`：macro-avg
- `verdict_consistency`：suite 级一致率
- `per_fixture_trials`：每条 fixture 的 trial 分解

`to_markdown()` 新增 "Multi-Trial Statistics" 章节，包含 suite 级统计表和 per-fixture 分解表。

---

## 十二、高阶问题

### Q43: 如果让你从零设计一个 Agent 评测体系，你会怎么做？

**答:**

遵循 Anthropic 的 8 步路线：

1. **尽早开始**：不要等 Agent 完善了才建评测。20 条 fixture 就够起步。
2. **从真实 bug 开始**：手工测试中发现的 bug 就是最好的 fixture 候选。
3. **写无歧义的 Task + 参考解**：YAML schema + golden_findings，每条都有明确的标准答案。
4. **正负面平衡**：Golden 测能力（别漏），Benign 测精确率（别乱报），Injection 测安全（别被骗），Adversarial 测 LLM 弱点。
5. **稳健 Harness + 稳定环境**：确定性层纯函数；LLM 层 temperature 固定；diff 截断逻辑一致。
6. **认真设计 Grader**：Code-based 管确定性维度，LLM-as-judge 管语义维度，Human 做校准。
7. **查看 Transcript**：失败用例保存完整 messages，做失败模式分类。
8. **监控饱和 + 持续维护**：baseline.json + history 趋势；反饱和监控；评测 PR 流程 + ownership。

### Q44: 你的评测体系有哪些不足？如果继续做你会怎么改进？

**答:**

**不足 1：数据集规模偏小**
- 45 条 fixture 对"完整评测体系"偏少。真实生产可能需要 200+ 条。
- 改进：从线上 PR 中抽取真实案例，自动生成 fixture 骨架后人工标注。

**不足 2：语言覆盖单一**
- Python 为主，JS 只有 3 条，没有 Go/Rust/Java。
- 改进：按语言分子集，每种子集至少 20 条覆盖该语言的典型安全问题。

**不足 3：LLM-as-judge 未实际跑通**
- judge.py 有代码但 baseline.json 中没有 LLM-as-judge 的结果。
- 改进：跑通 judge，做 LLM 评分与人工评分的相关性分析。

**不足 4：缺少失败模式分类体系**
- 文档 Step 6（查看 transcript）标为待建。
- 改进：建立 failure taxonomy——"漏检规则" / "过度报告" / "verdict 映射错误" / "解析失败"，统计各类占比。

**不足 5：置信区间缺失**
- 45 条 fixture 的指标没有置信区间，Precision 0.64 在 20 条上的 95% CI 约 [0.44, 0.81]，区间很宽。
- 改进：用 bootstrap 或 Wilson score interval 计算置信区间，避免"达标/不达标"的确定性误判。

### Q45: 如果有人质疑你的评测数据不够有说服力，你怎么回应？

**答:**

三个层面回应：

1. **方法论可信**：评测体系基于 Anthropic 公开方法论，8 步路线图有明确落地状态。三类 Grader 组合、pass@k/pass^k、正交评测都是业界标准做法，不是自己发明的。

2. **数据可复现**：确定性层 45 条 fixture 全部开源，跑一次零成本秒级完成，任何人可以验证。baseline.json 与文档数据完全一致，经交叉验证。

3. **诚实面对不足**：LLM 层基线只有单 trial（已改进为多 trial），样本量 45 条确实偏小（95% CI 宽），LLM-as-judge 未跑通。这些不足不回避——知道哪里不够好，比假装完美更有说服力。

关键原则：**评测的价值不在于"证明我们很好"，而在于"知道我们哪里不好"。** Precision 0.30 的基线不丢人，没有评测只能盲飞才丢人。

### Q46: 你认为 Agent 评测和传统软件测试最大的区别是什么？

**答:**

最大的区别是**确定性前提的消失**。

传统软件测试：输入不变 → 输出不变 → 断言 `assertEqual(expected, actual)` → pass/fail。

Agent 评测：输入不变 → 输出可能变 → 不能用精确断言 → 需要统计方法。

这个区别带来三个连锁后果：

1. **必须多 trial**：跑一次的 pass 不算数，需要 pass@k / pass^k 量化稳定性和能力上限。
2. **需要统计指标**：从 pass/fail 变成 precision/recall/F1，从"对不对"变成"有多大概率对"。
3. **Grader 成为一等公民**：传统测试断言是代码写死的，Agent 评测需要三类 Grader（Code/LLM/Human），因为有些维度代码判不了。

一句话：**传统测试是"确定性系统的不确定性发现"（bug 是不确定的，但一旦发现就能精确复现），Agent 评测是"不确定性系统的确定性评估"（输出是不确定的，但通过统计方法得到确定的结论）。**

### Q47: 如果模型升级了（比如从 GPT-4 到 GPT-5），你的评测体系怎么帮你决策？

**答:**

流程：

1. **固定 Harness**：不改 graph 配置、不改 prompt、不改 verdict 逻辑。
2. **跑全量评测**：45 条 fixture × 5 trials，用 `--baseline-key llm_gpt5_5trial` 存储。
3. **对比 baseline**：和 `llm_5trial`（GPT-4 基线）逐维度对比：
   - Precision 升了还是降了？（FP 更多还是更少）
   - Recall 升了还是降了？（漏检更多还是更少）
   - Verdict Accuracy 达标了吗？（≥ 0.90）
   - pass^5 升了还是降了？（稳定性变了）
   - Benign Precision 变了吗？（对干净代码的表现）
4. **看混淆矩阵**：哪些用例从"错"变"对"了？哪些从"对"变"错"了？
5. **查 transcript**：新模型在哪些类型的代码上表现更好/更差？

决策标准：如果 Verdict Accuracy 达标 + pass^5 不降 + 没有"对的变错"的回归 → 切换。否则看 trade-off 是否可接受。

### Q48: 你觉得这个评测体系最难的地方是什么？

**答:**

**不是写代码，是定义"什么算对"。**

比如一条 finding 说"第 35 行有性能问题"，但 golden 里写的是"第 33 行有安全问题"——这算 TP 还是 FP？line 差了 2 行但在容差范围内，但 rule_id 不同（性能 vs 安全）。你需要在"太严"和"太松"之间找到平衡：

- 太严：LLM 报的只要和 golden 不完全一样就算 FP → precision 被低估，Agent 看起来比实际差
- 太松：LLM 报的只要 file 对就算 TP → precision 被高估，Agent 看起来比实际好

CR Agent 的选择是 ±2 line tolerance + rule_id 语义等价（LLM 规则放宽到只看 file+line）。这个选择没有标准答案，只能通过实测和人工抽检来校准。

另一个难处是**数据集的对抗性设计**。DS-Adversarial 的 4 条用例需要你站在 LLM 的角度思考——"LLM 看到什么关键词会误报"。这需要对 LLM 行为模式的深入理解，不是简单的"代码有 bug 就写进 golden"。

---

> **面试策略提示**: 面试中不需要背诵每道题的完整答案。核心要记住的是：
> 1. **为什么**需要评测（Agent 非确定性 → 传统测试失效 → 必须多 trial 统计）
> 2. **怎么**设计评测（4 子集 + 3 类 Grader + 9 维度 + pass@k/pass^k）
> 3. **踩过什么坑**（Precision 0.30 的根因、去重引入的新问题、样本量太小）
> 4. **还差什么**（LLM-as-judge 未跑通、transcript 分析未建、置信区间缺失）
>
> 面试官最看重的不是"你做了多完美"，而是"你能不能说清楚为什么这么做、踩了什么坑、下一步怎么改"。
