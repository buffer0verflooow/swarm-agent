# 报告：TrueCyber 逆向工具链分析

**来源：** blog.truecyber.world — 《The Reverse Engineering Toolkit: CallHook, TrueDiffing, ExportFinder and NetHook》
**作者：** Charles F. Hamilton（Mr.Un1k0d3r）/ TrueCyber Inc.
**日期：** 2026-07-28
**报告生成：** 2026-07-29
**分析状态：** ✅ 完成

---

## 一、目标概述

TrueCyber 发布了一套四款 Windows 逆向工具，定位为"小、锐利的单问靶向工具"，每款对应二进制分析中的一个核心问题：

| 工具 | 回答的问题 | 核心能力 |
|------|-----------|----------|
| **CallHook** | 它到底在调用什么？ | 进程注入后实时记录所有调用（包括动态解析、内部 helper、指针间接调用），支持单步跟踪模式；显示调用偏移直接定位反汇编器中的 call site；加载器追踪可捕获侧加载、延迟导入、手动映射行为 |
| **TrueDiffing** | 什么东西变了？ | 函数级二进制差异比对（非字节级），自动跨版本匹配函数，分类为修改/新增/删除/未变；支持指令级 diff 显示 opcode 字节；支持 CFG 图视图对比；支持字符串对比；**不限于 Windows 二进制**（截图展示的是 Linux 目标） |
| **ExportFinder** | 它真正导入的是什么？ | 读取导出表，显示序号、RVA、名称和转发目标（forwarder）；可辨识 forwarded exports（如 `kernel32.CreateProcessA` → `KERNEL32.DLL.CreateProcessA` → `api-ms-win-*`）；**完全免费且无许可证限制** |
| **NetHook** | 它发送了什么？ | API 层钩子捕获/检查/重写进程网络流量；绕过证书固定、自定义 TLS 栈、proxy 不兼容等问题；支持 Capture 模式（观察）和 Intercept 模式（拦截修改后放行/丢弃）；被加密的流量在缓冲区层以明文呈现 |

---

## 二、工具协同工作流

文章演示了一个典型的实际场景（分析一个行为可疑的更新程序）：
1. CallHook → 发现调用栈异常，定位到可疑函数
2. TrueDiffing → 对比新老版本，确定哪个代码分支被添加/修改
3. ExportFinder → 解析可疑函数的真实来源（是否为 forwarded export，指向哪个模块）
4. NetHook → 确认进程实际发送了什么数据（明文可见）

整个过程耗时数分钟，每步产出可交付 artifact。不需要断点等待。

---

## 三、配套培训课程

- 课程名称：**Reverse Engineering Training（暂无具体名称公布）**
- 定位：动手实操型，围绕这四款工具编写，非幻灯片式教学
- 状态：Coming soon，课程大纲已在线
- 发布渠道优先：**Newsletter**（邮件列表优先于博客）

---

## 四、可信度评估

| 维度 | 评估 | 依据 |
|------|------|------|
| 作者身份 | ✅ 高可信 | Mr.Un1k0d3r（Charles F. Hamilton），LinkedIn 可验证的安全研究员身份 |
| 技术内容 | ✅ 可信 | 截图展示真实工具界面（CallHook 显示 3,593 次调用、TrueDiffing 展示 Linux 二进制比对），技术描述具体无夸大 |
| 商业化程度 | ⚠️ 中等 | 三款工具收费（CallHook/TrueDiffing/NetHook），ExportFinder 免费；定价未公布；Training 为商业课程 |
| 新颖性 | ✅ 中高 | Hook/动态分析/Diff 并非新概念，但单问靶向工具的定位和工具链编排方式有独特视角 |
| 宣传成分 | ⚠️ 中等 | 文章结尾导向 Newsletter 和 Training 购买；但技术内容本身有实质 |

---

## 五、安全洞察与利用方向

1. **CallHook 的价值**：对黑盒 API 审计和恶意软件分析场景有实际帮助。Loader tracing 功能可捕获侧加载和手动映射——这在红队检测规避和蓝队取证中都是关键信号。
2. **TrueDiffing 的亮点**：支持非 Windows 目标（截图显示 Linux ELF），这在多平台固件/Linux 嵌入式/IoT 逆向场景中更实用于 Bug Bounty 场景。函数匹配+指令对齐的能力填补了 Ghidra/Binary Ninja diff 插件的大部分痛点。
3. **NetHook 的 Intercept 模式**：可用于厚客户端 API 篡改测试（修改请求/重放攻击/绕过客户端校验），比 Burp 代理更底层，不受代理配置和 SSL Pinning 限制。
4. **ExportFinder 的困境**：文章指出 Windows 7 vs 10/11 的 API 集差异巨大，不同 Windows 版本的 `ApiSetSchema.dll` 映射表可导致 DLL 解析失败。这对跨版本漏洞利用的兼容性判断有实际参考价值。

---

## 六、不确定性

- ❓ 定价信息未公开（CallHook/TrueDiffing/NetHook 的许可模式和价格）
- ❓ ExportFinder 免费但其他三款是否提供试用/Trial 未知
- ❓ 工具是否支持 32-bit 进程未明确说明
- ❓ Training 课程的具体日期和价格未公布

---

## 七、结论

TrueCyber 的这套工具链定位清晰：不追求大而全的逆向套件，而是围绕逆向工程师日常工作中的四个关键问题（调用了什么、什么变了、导入了什么、发送了什么）各做一把精准的工具。技术可信度较高，作者 Mr.Un1k0d3r 在安全社区有公信力。

**对于 Bug Bounty / 红队场景：** NetHook 的 API 层拦截和 CallHook 的实时调用追踪有直接实用价值，特别是在厚客户端测试、自定义加密封装协议逆向等场景。TrueDiffing 的跨平台支持（Linux ELF）优于大多数竞品。

**建议关注：** Newsletter 订阅跟踪 Training 发布时间和工具许可策略。ExportFinder 可作为免费的长期保留工具。
