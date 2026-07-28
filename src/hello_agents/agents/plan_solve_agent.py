"""Plan-and-Solve reasoning strategy backed by Agent Core."""

from __future__ import annotations

import ast
from typing import Optional

from hello_agents.core.agent import Agent
from hello_agents.core.config import Config
from hello_agents.core.contracts import (
    AgentLoopContext,
    AgentRunResult,
    Plan,
    ToolCall,
)
from hello_agents.core.conversation_manager import ConversationManager
from hello_agents.core.execution import ControlledExecution
from hello_agents.core.llm import HelloAgentsLLM
from hello_agents.core.runtime import AgentCore
from hello_agents.tools.registry import ToolRegistry


DEFAULT_PLANNER_PROMPT = """\
Break the task into a Python list of concise reasoning steps.

Task: {question}

Return only a list such as:
["inspect the requirements", "derive the answer"]
"""

DEFAULT_EXECUTOR_PROMPT = """\
Solve the current reasoning step. Do not claim external actions or tool results.

Original task:
{question}

Plan:
{plan}

Completed steps:
{history}

Current step:
{current_step}
"""


class Planner:
    """Convert one objective into a bounded list of reasoning steps."""

    def __init__(
        self,
        core: AgentCore,
        prompt_template: Optional[str] = None,
        max_steps: int = 10,
    ) -> None:
        self.core = core
        self.prompt_template = prompt_template or DEFAULT_PLANNER_PROMPT
        self.max_steps = max_steps

    def plan(self, question: str, **kwargs) -> list[str]:
        response = self.core.reason(
            [
                {
                    "role": "user",
                    "content": self.prompt_template.format(
                        question=question
                    ),
                }
            ],
            **kwargs,
        )
        if "```" in response:
            blocks = response.split("```")
            response = blocks[1].removeprefix("python").strip()
        try:
            raw = ast.literal_eval(response)
        except (SyntaxError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        return [
            str(step).strip()
            for step in raw[: self.max_steps]
            if str(step).strip()
        ]


class Executor:
    """Execute plan steps as reasoning, never as implicit side effects."""

    def __init__(
        self,
        core: AgentCore,
        prompt_template: Optional[str] = None,
    ) -> None:
        self.core = core
        self.prompt_template = prompt_template or DEFAULT_EXECUTOR_PROMPT
        self.last_results: list[str] = []

    def execute(
        self,
        question: str,
        plan: list[str],
        **kwargs,
    ) -> str:
        self.last_results = []
        for step in plan:
            history = "\n".join(
                f"{index}. {result}"
                for index, result in enumerate(
                    self.last_results,
                    start=1,
                )
            )
            result = self.core.reason(
                [
                    {
                        "role": "user",
                        "content": self.prompt_template.format(
                            question=question,
                            plan=plan,
                            history=history or "None",
                            current_step=step,
                        ),
                    }
                ],
                **kwargs,
            )
            self.last_results.append(result)
        return self.last_results[-1] if self.last_results else ""


class PlanAndSolveAgent(Agent):
    """Plan first, then solve each reasoning step through Agent Core."""

    def __init__(
        self,
        name: str,
        llm: HelloAgentsLLM,
        system_prompt: Optional[str] = None,
        config: Optional[Config] = None,
        conversation_manager: Optional[ConversationManager] = None,
        planner_prompt: Optional[str] = None,
        executor_prompt: Optional[str] = None,
        max_steps: int = 10,
        agent_core: Optional[AgentCore] = None,
    ) -> None:
        super().__init__(
            name=name,
            llm=llm,
            system_prompt=system_prompt,
            config=config,
            conversation_manager=conversation_manager,
        )
        self.agent_core = agent_core or AgentCore(
            ControlledExecution(ToolRegistry()),
            llm=llm,
            conversation_manager=conversation_manager,
        )
        self.conversation_manager = self.agent_core.conversation_manager
        self.planner = Planner(
            self.agent_core,
            planner_prompt,
            max_steps=max_steps,
        )
        self.executor = Executor(self.agent_core, executor_prompt)
        self.last_plan: list[str] = []

    def run(self, input_text: str, **kwargs) -> str:
        conversation_id = kwargs.pop("conversation_id", None)
        self.last_plan = self.planner.plan(input_text, **kwargs)
        if not self.last_plan:
            response = "Unable to generate a valid action plan."
        else:
            response = self.executor.execute(
                input_text,
                self.last_plan,
                **kwargs,
            )
        self._save_conversation_messages(
            input_text,
            response,
            conversation_id,
        )
        return response

    def run_tool_plan(
        self,
        objective: str,
        steps: list[ToolCall] | tuple[ToolCall, ...],
        *,
        stop_on_policy_denial: bool = True,
    ) -> AgentRunResult:
        """Execute explicit ToolCalls through the same controlled core."""
        return self.agent_core.run_plan(
            Plan(objective=objective, steps=tuple(steps)),
            stop_on_policy_denial=stop_on_policy_denial,
        )

    @staticmethod
    def bounded_plan(context: AgentLoopContext) -> tuple[str, ...]:
        """Describe the executable remainder of the already bounded Plan."""
        return tuple(
            action.purpose or action.tool_name
            for action in context.remaining_actions
        )
