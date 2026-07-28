"""工具基类"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List

from pydantic import BaseModel

from hello_agents.core.contracts import ToolEffect


class ToolParameter(BaseModel):
    """工具参数定义"""

    name: str
    type: str
    description: str
    required: bool = True
    default: Any = None


class Tool(ABC):
    """工具基类"""

    def __init__(
        self,
        name: str,
        description: str,
        *,
        effect: ToolEffect = ToolEffect.READ,
    ):
        self.name = name
        self.description = description
        self.effect = effect

    @abstractmethod
    def run(self, parameters: Dict[str, Any]) -> str:
        """执行工具"""
        pass

    @abstractmethod
    def get_parameters(self) -> List[ToolParameter]:
        """获取工具参数定义"""
        pass

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        """验证参数"""
        required_params = [p.name for p in self.get_parameters() if p.required]
        return all(param in parameters for param in required_params)

    def effective_effect(self, parameters: Dict[str, Any]) -> ToolEffect:
        """Return the effect for one invocation.

        Most tools have one fixed effect.  Environment tools that can either
        fill a form or submit it may derive the effect from their already
        validated invocation context.  ControlledExecution remains the only
        caller of this hook, so a tool cannot use it to bypass the policy gate.
        """
        return self.effect

    def to_openai_schema(self) -> Dict[str, Any]:
        """转换为 OpenAI function calling schema 格式"""
        parameters = self.get_parameters()
        properties = {}
        required = []

        for param in parameters:
            prop = {"type": param.type, "description": param.description}
            if param.default is not None:
                prop["description"] = f"{param.description} (默认: {param.default})"
            if param.type == "array":
                prop["items"] = {"type": "string"}
            properties[param.name] = prop
            if param.required:
                required.append(param.name)

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    def __str__(self) -> str:
        return f"Tool(name={self.name})"

    def __repr__(self) -> str:
        return self.__str__()
