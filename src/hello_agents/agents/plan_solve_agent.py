"""Plan-and-solve agent implementation for HelloAgents."""

from __future__ import annotations

import ast
from typing import Optional

from hello_agents.core.agent import Agent
from hello_agents.core.config import Config
from hello_agents.core.llm import HelloAgentsLLM


DEFAULT_PLANNER_PROMPT = """
You are a planning agent. Break the task into a Python list of concise executable steps.

Task: {question}

Return only:
```python
["step 1", "step 2"]
```
"""


DEFAULT_EXECUTOR_PROMPT = """
Execute the current step using the original task, plan, and history.

Original task:
{question}

Plan:
{plan}

History:
{history}

Current step:
{current_step}

Return the result for this step only.
"""


class Planner:
    def __init__(self, llm: HelloAgentsLLM, prompt_template: Optional[str] = None):
        self.llm = llm
        self.prompt_template = prompt_template or DEFAULT_PLANNER_PROMPT

    def plan(self, question: str, **kwargs) -> list[str]:
        prompt = self.prompt_template.format(question=question)
        response = self.llm.invoke([{"role": "user", "content": prompt}], **kwargs) or ""
        try:
            if "```python" in response:
                response = response.split("```python", 1)[1].split("```", 1)[0].strip()
            parsed = ast.literal_eval(response)
            return parsed if isinstance(parsed, list) else []
        except (SyntaxError, ValueError):
            return []


class Executor:
    def __init__(self, llm: HelloAgentsLLM, prompt_template: Optional[str] = None):
        self.llm = llm
        self.prompt_template = prompt_template or DEFAULT_EXECUTOR_PROMPT

    def execute(self, question: str, plan: list[str], **kwargs) -> str:
        history = ""
        result = ""
        for index, step in enumerate(plan, 1):
            prompt = self.prompt_template.format(
                question=question,
                plan=plan,
                history=history or "None",
                current_step=step,
            )
            result = self.llm.invoke([{"role": "user", "content": prompt}], **kwargs) or ""
            history += f"Step {index}: {step}\nResult: {result}\n\n"
        return result


class PlanAndSolveAgent(Agent):
    """Agent that first plans a multi-step task, then executes the steps."""

    def __init__(
        self,
        name: str,
        llm: HelloAgentsLLM,
        system_prompt: Optional[str] = None,
        config: Optional[Config] = None,
        planner_prompt: Optional[str] = None,
        executor_prompt: Optional[str] = None,
    ):
        super().__init__(name=name, llm=llm, system_prompt=system_prompt, config=config)
        self.planner = Planner(llm, planner_prompt)
        self.executor = Executor(llm, executor_prompt)

    def run(self, input_text: str, **kwargs) -> str:
        plan = self.planner.plan(input_text, **kwargs)
        if not plan:
            return "Unable to generate a valid action plan."
        return self.executor.execute(input_text, plan, **kwargs)
