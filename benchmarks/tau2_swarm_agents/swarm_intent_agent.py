"""SwarmIntentAgent — IntentTracker 蜂群 agent v2 (τ-bench)。

v1 (SwarmPolicyAgent) 教训: deepseek-chat 本身 policy 合规好, 验证器对
airline 域价值有限。真实失败模式:
  1. 任务 7: 对话中途插入新诉求(查其他航班总费用), agent 部分处理未完成
     → 状态跟踪缺失
  2. 任务 1: 取消+退款资格判定, 单 agent 直接取消(违规)

v2 设计: 每轮独立 **intent verifier** 维护"用户未满足诉求清单"(跨轮状态),
对照 agent 候选响应检查遗漏 → 遗漏则修正轮注入 missing intents 重生成。
policy 检查并入同一 verifier (敏感动作审查)。

成本: 每轮 +1 次验证器调用 (与 v1 相同), 但验证器输出更有用。
"""
import json
import re
from typing import List, Optional

from loguru import logger

from tau2.agent.base.llm_config import LLMConfigMixin
from tau2.agent.base_agent import HalfDuplexAgent
from tau2.agent.llm_agent import LLMAgent, LLMAgentState
from tau2.data_model.message import (
    AssistantMessage,
    MultiToolMessage,
    SystemMessage,
    UserMessage,
)
from tau2.environment.tool import Tool
from tau2.utils.llm_utils import generate

VERIFIER_PROMPT = """You are the INTENT TRACKER inside a customer-service swarm.
You maintain the list of the user's UNFULFILLED requests across the conversation,
and check whether the agent's latest response addresses them.

<policy>
{policy}
</policy>

<conversation_history>
{history}
</conversation_history>

<latest_user_message>
{user_message}
</latest_user_message>

<agent_candidate_response>
{candidate}
</agent_candidate_response>

<current_known_intents>
{intents}
</current_known_intents>

Do two things:
1. UPDATE the intent list: extract every user request from the history that has
   NOT yet been fulfilled (e.g. cancel reservation X, check total cost of other
   flights, upgrade to business, check refund eligibility). Remove intents the
   agent has completed (confirmed by tool results or user acknowledgement).
   The latest user message may introduce NEW intents — include them.
2. CHECK the candidate response on TWO axes:
   a. PROGRESS: does it make progress on at least one current intent?
   b. HARD POLICY: does it perform or promise an action the policy forbids?
      CRITICAL: even if the user EXPLICITLY requests it, actions like refunds
      outside the policy window, cancellations that forfeit refund rights
      without telling the user, or sharing another user's data are FORBIDDEN.
      The agent must REFUSE and explain, or transfer to a human agent — it must
      NEVER comply with a policy-forbidden request just because the user asked.
   If the candidate violates a HARD POLICY rule, compliant MUST be false even
   when it makes progress on intents.

Output ONLY JSON:
{{"intents": ["unfulfilled intent 1", "..."],
  "compliant": true/false,
  "missing": ["intent the candidate should address but doesn't, or the HARD POLICY rule it violates"],
  "guidance": "concrete next action the agent should take (e.g. refuse refund, explain 24h policy, transfer to human)"}}"""

REVISE_PROMPT = """Your response was flagged by the intent tracker.

<missing>
{missing}
</missing>

<guidance>
{guidance}
</guidance>

Address the missing intents / fix the violation. If a requested action is not
allowed by policy, tell the user why and offer a CONCRETE ALTERNATIVE (rebooking,
partial options, explaining the policy detail, escalating the case) — do NOT
just repeat "your request has been noted" or insist on transferring when the
user refuses. Never perform the disallowed action and never drop the user's
other pending requests. If the user has repeated the same request more than
twice, give them a DIFFERENT concrete option than your previous replies.

Generate the corrected response now. Same JSON format as before."""


class SwarmIntentAgent(LLMAgent):
    """IntentTracker 蜂群: 每轮 intent verifier 维护跨轮诉求清单 + 检查遗漏。"""

    def __init__(
        self,
        tools: List[Tool],
        domain_policy: str,
        llm: str,
        llm_args: Optional[dict] = None,
        verifier_llm: Optional[str] = None,
        verifier_llm_args: Optional[dict] = None,
        max_revisions: int = 2,
        policy_max_chars: int = 6000,
    ):
        super().__init__(tools=tools, domain_policy=domain_policy, llm=llm, llm_args=llm_args)
        self.verifier_llm = verifier_llm or llm
        self.verifier_llm_args = verifier_llm_args or (llm_args or {})
        self.max_revisions = max_revisions
        self.policy_snippet = domain_policy[:policy_max_chars]
        self.intents: List[str] = []
        self.stats = {"verified": 0, "violations": 0, "revisions": 0, "intents_seen": 0}

    def _verify(self, user_message: str, candidate: str, history: str) -> dict:
        """intent verifier: 更新诉求清单 + 检查候选遗漏"""
        prompt = VERIFIER_PROMPT.format(
            policy=self.policy_snippet,
            history=history[-5000:],
            user_message=user_message,
            candidate=candidate[:2000],
            intents=json.dumps(self.intents, ensure_ascii=False) if self.intents else "[]",
        )
        try:
            resp = generate(
                model=self.verifier_llm,
                messages=[UserMessage(role="user", content=prompt)],
                call_name="intent_verify",
                **self.verifier_llm_args,
            )
            text = resp.content or ""
            m = re.search(r"\{.*\}", text, re.S)
            if m:
                verdict = json.loads(m.group(0))
                # 更新 intents (跨轮状态)
                if isinstance(verdict.get("intents"), list):
                    self.intents = [str(i)[:200] for i in verdict["intents"]]
                return verdict
            return {"intents": self.intents, "compliant": True, "missing": [], "guidance": ""}
        except Exception as e:
            logger.warning(f"verifier error: {e}")
            return {"intents": self.intents, "compliant": True, "missing": [], "guidance": ""}

    def _generate_next_message(
        self, message: UserMessage, state: LLMAgentState
    ) -> AssistantMessage:
        if isinstance(message, MultiToolMessage):
            # 工具结果轮: 主 agent 决策 (验证器跳过, 下一轮用户消息再查)
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
            for m in state.messages[-10:]
        )
        user_text = str(getattr(message, "content", ""))

        candidate_msg: AssistantMessage = None
        last_candidate_text = ""
        for attempt in range(self.max_revisions + 1):
            messages = state.system_messages + state.messages
            candidate_msg = generate(
                model=self.llm,
                tools=self.tools,
                messages=messages,
                call_name="agent_response",
                **self.llm_args,
            )
            self.stats["verified"] += 1

            candidate_text = candidate_msg.content or json.dumps(
                [tc.dict() for tc in (candidate_msg.tool_calls or [])])[:1500]
            # 循环保护: 候选与上次相同 (修正无效) → 直接输出
            if last_candidate_text and candidate_text == last_candidate_text:
                return candidate_msg
            last_candidate_text = candidate_text

            verdict = self._verify(user_text, candidate_text, history)
            self.stats["intents_seen"] = max(self.stats["intents_seen"], len(self.intents))

            if verdict.get("compliant", True) or not verdict.get("missing"):
                return candidate_msg

            self.stats["violations"] += 1
            self.stats["revisions"] += 1
            missing = str(verdict.get("missing", ""))[:600]
            guidance = str(verdict.get("guidance", ""))[:600]
            revise_prompt = REVISE_PROMPT.format(missing=missing, guidance=guidance)
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
            # 循环保护 2: 修正后与修正前相同 → 停止
            candidate_text2 = candidate_msg.content or json.dumps(
                [tc.dict() for tc in (candidate_msg.tool_calls or [])])[:1500]
            if candidate_text2 == candidate_text:
                return candidate_msg
            # 修正后再验证一次
            verdict2 = self._verify(user_text, candidate_text2, history)
            if verdict2.get("compliant", True) or not verdict2.get("missing"):
                return candidate_msg

        return candidate_msg


def create_swarm_intent_agent(tools, domain_policy, **kwargs):
    """Factory: SwarmIntentAgent"""
    return SwarmIntentAgent(
        tools=tools,
        domain_policy=domain_policy,
        llm=kwargs.get("llm"),
        llm_args=kwargs.get("llm_args"),
        verifier_llm=kwargs.get("verifier_llm"),
        verifier_llm_args=kwargs.get("verifier_llm_args"),
        max_revisions=kwargs.get("max_revisions", 2),
    )
