"""SwarmPolicyAgent — PolicyGuard 蜂群 agent (τ-bench)。

每轮: 主 LLM 生成候选响应 → policy 验证器 (独立 LLM) 审查候选是否违反
domain policy → 违规则修正轮 (violation + guidance 反馈给主 LLM 重生成)。

假说: τ-bench 的主要失败模式是 agent 违反 policy (不验证身份、忽略
规则细节)。独立验证器每轮显式审查 policy 能捕获这些违规 → 蜂群分工
提高 policy 合规率。
"""
import json
import re
from typing import List, Optional

from loguru import logger

from tau2.agent.base.llm_config import LLMConfigMixin
from tau2.agent.base_agent import HalfDuplexAgent
from tau2.agent.llm_agent import LLMAgent, LLMAgentState
from tau2.data_model.message import (
    APICompatibleMessage,
    AssistantMessage,
    MultiToolMessage,
    SystemMessage,
    UserMessage,
)
from tau2.environment.tool import Tool
from tau2.utils.llm_utils import generate

VERIFIER_PROMPT = """You are a POLICY COMPLIANCE VERIFIER inside a customer-service swarm.
Your job: check whether the agent's candidate response VIOLATES the customer-service policy.

<policy>
{policy}
</policy>

<conversation_so_far>
{history}
</conversation_so_far>

<latest_user_message>
{user_message}
</latest_user_message>

<candidate_response>
{candidate}
</candidate_response>

Check for violations like:
- Sharing info without proper identity verification (auth)
- Giving refunds/waivers outside policy rules
- Making changes to accounts/orders without required conditions
- Promising things policy forbids
- Ignoring a required verification step

Output ONLY JSON: {{"compliant": true/false, "violation": "specific policy rule violated or empty", "guidance": "what the agent should do instead or empty"}}"""

REVISE_PROMPT = """Your previous response was flagged as a POLICY VIOLATION.

<violation>
{violation}
</violation>

<guidance>
{guidance}
</guidance>

Original instructions remain: follow the policy strictly. If the requested action
violates policy, tell the user why and what they can do instead — do NOT perform
the violation.

Generate the corrected response now. Output the same JSON format as before
(message text or tool call)."""


class SwarmPolicyAgent(LLMAgent):
    """PolicyGuard 蜂群: 主 agent 生成 → policy 验证器审查 → 违规则修正。"""

    def __init__(
        self,
        tools: List[Tool],
        domain_policy: str,
        llm: str,
        llm_args: Optional[dict] = None,
        verifier_llm: Optional[str] = None,
        verifier_llm_args: Optional[dict] = None,
        max_revisions: int = 2,
        verifier_policy_max_chars: int = 6000,
    ):
        super().__init__(tools=tools, domain_policy=domain_policy, llm=llm, llm_args=llm_args)
        self.verifier_llm = verifier_llm or llm
        self.verifier_llm_args = verifier_llm_args or (llm_args or {})
        self.max_revisions = max_revisions
        self.policy_snippet = domain_policy[:verifier_policy_max_chars]
        self.stats = {"verified": 0, "violations": 0, "revisions": 0}

    def _verify_policy(self, user_message: str, candidate: str, history: str) -> dict:
        """验证器: 审查候选响应的 policy 合规性"""
        prompt = VERIFIER_PROMPT.format(
            policy=self.policy_snippet,
            history=history[-4000:],
            user_message=user_message,
            candidate=candidate[:2000],
        )
        try:
            resp = generate(
                model=self.verifier_llm,
                messages=[UserMessage(role="user", content=prompt)],
                call_name="policy_verify",
                **self.verifier_llm_args,
            )
            text = resp.content or ""
            m = re.search(r"\{.*\}", text, re.S)
            if m:
                return json.loads(m.group(0))
            return {"compliant": True, "violation": "", "guidance": f"verifier no JSON: {text[:100]}"}
        except Exception as e:
            logger.warning(f"verifier error: {e}")
            return {"compliant": True, "violation": "", "guidance": ""}

    def _generate_next_message(
        self, message: UserMessage, state: LLMAgentState
    ) -> AssistantMessage:
        """主生成 → policy 验证 → 修正 (最多 max_revisions 轮)"""
        # 输入消息入 state (与 LLMAgent 一致)
        if isinstance(message, MultiToolMessage):
            # 工具结果轮: 主 agent 决策 (验证器对工具结果轮意义小, 直接主生成)
            # 注意: 不能调 super() (会重复 extend tool_messages → 孤立 ToolMessage)
            state.messages.extend(message.tool_messages)
            assistant_message = generate(
                model=self.llm,
                tools=self.tools,
                messages=state.system_messages + state.messages,
                call_name="agent_response",
                **self.llm_args,
            )
            return assistant_message
        else:
            state.messages.append(message)

        history = "\n".join(
            f"{'AGENT' if isinstance(m, AssistantMessage) else 'USER'}: {str(getattr(m, 'content', ''))[:200]}"
            for m in state.messages[-8:]
        )
        user_text = str(getattr(message, "content", ""))

        candidate_msg: AssistantMessage = None
        for attempt in range(self.max_revisions + 1):
            # 主生成 (本次候选)
            messages = state.system_messages + state.messages
            candidate_msg = generate(
                model=self.llm,
                tools=self.tools,
                messages=messages,
                call_name="agent_response",
                **self.llm_args,
            )
            self.stats["verified"] += 1

            # policy 验证器 (所有轮都查)
            candidate_text = candidate_msg.content or json.dumps(
                [tc.dict() for tc in (candidate_msg.tool_calls or [])])[:1500]
            verdict = self._verify_policy(user_text, candidate_text, history)

            if verdict.get("compliant", True):
                return candidate_msg

            # 违规 → 修正轮
            self.stats["violations"] += 1
            self.stats["revisions"] += 1
            violation = str(verdict.get("violation", ""))[:500]
            guidance = str(verdict.get("guidance", ""))[:500]
            revise_prompt = REVISE_PROMPT.format(violation=violation, guidance=guidance)
            # 把修正指令作为 system 追加 (不污染历史)
            revise_messages = [
                SystemMessage(role="system", content=self.system_prompt + "\n\n" + revise_prompt)
            ] + list(state.messages)
            candidate_msg = generate(
                model=self.llm,
                tools=self.tools,
                messages=revise_messages,
                call_name="agent_response_revise",
                **self.llm_args,
            )
            # 修正后再验证一次 (双保险)
            candidate_text2 = candidate_msg.content or json.dumps(
                [tc.dict() for tc in (candidate_msg.tool_calls or [])])[:1500]
            verdict2 = self._verify_policy(user_text, candidate_text2, history)
            if verdict2.get("compliant", True):
                return candidate_msg

        return candidate_msg


def create_swarm_policy_agent(tools, domain_policy, **kwargs):
    """Factory: SwarmPolicyAgent"""
    return SwarmPolicyAgent(
        tools=tools,
        domain_policy=domain_policy,
        llm=kwargs.get("llm"),
        llm_args=kwargs.get("llm_args"),
        verifier_llm=kwargs.get("verifier_llm"),
        verifier_llm_args=kwargs.get("verifier_llm_args"),
        max_revisions=kwargs.get("max_revisions", 2),
    )
