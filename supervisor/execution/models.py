from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class PlanInput(BaseModel):
    kind: Literal["text", "url", "file"] = "text"
    value: str = Field(min_length=1, max_length=200000)
    label: str = Field(default="", max_length=200)


class PlanStep(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    title: str = Field(min_length=1, max_length=200)
    kind: Literal["read_input", "model", "tool", "artifact"]
    dependencies: list[str] = Field(default_factory=list, max_length=80)
    input_index: int | None = Field(default=None, ge=0)
    operation: str = Field(default="", max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)
    instruction: str = Field(default="", max_length=12000)


class PlanDraft(BaseModel):
    goal: str = Field(min_length=1, max_length=4000)
    work_scope: str = Field(default="", max_length=4000)
    inputs: list[PlanInput] = Field(default_factory=list, max_length=40)
    steps: list[PlanStep] = Field(default_factory=list, max_length=120)

    @model_validator(mode="after")
    def validate_steps(self):
        if not self.steps:
            if not self.inputs:
                self.inputs = [PlanInput(value=self.goal, label="需求")]
            for index, item in enumerate(self.inputs):
                base = f"item{index + 1}"
                for suffix, kind, title, dependencies in (
                    ("read", "read_input", "读取", []),
                    ("process", "model", "处理", [f"{base}_read"]),
                    ("save", "artifact", "保存结果", [f"{base}_process"]),
                ):
                    self.steps.append(PlanStep(
                        id=f"{base}_{suffix}", title=f"{title}：{item.label or index + 1}",
                        kind=kind, dependencies=dependencies, input_index=index,
                    ))
        names = [step.id for step in self.steps]
        if len(names) != len(set(names)):
            raise ValueError("步骤编号重复")
        by_id = {step.id: step for step in self.steps}
        complete: set[str] = set()
        while len(complete) < len(names):
            ready = {
                key for key, step in by_id.items()
                if key not in complete and set(step.dependencies) <= complete
            }
            if not ready:
                raise ValueError("步骤依赖不存在或形成循环")
            complete.update(ready)
        for step in self.steps:
            if step.kind == "read_input" and step.input_index is None:
                raise ValueError("读取步骤必须指定 input_index")
            if step.input_index is not None and step.input_index >= len(self.inputs):
                raise ValueError("输入编号超出范围")
            if step.kind == "tool" and not step.operation:
                raise ValueError("工具步骤必须指定 operation")
        return self


TERMINAL_STEPS = {"succeeded", "failed", "cancelled", "outcome_unknown", "blocked"}
READ_ONLY_TOOLS = frozenset({"read_file", "list_dir", "search_in_files", "get_context_window", "list_active_tasks"})
