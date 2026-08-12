import os
import sys
import tempfile
from types import ModuleType
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

# `src.evaluator` imports the full model/agent stack for production.  These
# tests exercise only its task lifecycle, so replace those heavyweight modules
# before importing the evaluator.
factory_module = ModuleType("src.factory")
factory_module.MCPServiceFactory = object
model_config_module = ModuleType("src.model_config")
model_config_module.ModelConfig = object
agents_module = ModuleType("src.agents")
agents_module.AGENT_REGISTRY = {}
sys.modules["src.factory"] = factory_module
sys.modules["src.model_config"] = model_config_module
sys.modules["src.agents"] = agents_module

from src.evaluator import MCPEvaluator  # noqa: E402
from src.results_reporter import ResultsReporter, TaskResult  # noqa: E402


class EvaluatorCleanupTests(TestCase):
    def _evaluator(self, root: Path) -> MCPEvaluator:
        evaluator = MCPEvaluator.__new__(MCPEvaluator)
        evaluator.state_manager = Mock()
        evaluator.task_manager = Mock()
        evaluator.agent = Mock()
        evaluator.results_reporter = ResultsReporter()
        evaluator.base_experiment_dir = root
        evaluator.litellm_run_model_name = None
        return evaluator

    @staticmethod
    def _task() -> SimpleNamespace:
        return SimpleNamespace(
            name="reddit__task", category_id="reddit", task_id="task"
        )

    def test_cleanup_runs_after_agent_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            evaluator = self._evaluator(Path(temp_dir))
            task = self._task()
            evaluator.state_manager.set_up.return_value = True
            evaluator.state_manager.clean_up.return_value = True
            evaluator.task_manager.get_task_instruction.return_value = "instruction"
            evaluator.agent.execute_sync.side_effect = RuntimeError("agent failed")

            with self.assertRaisesRegex(RuntimeError, "agent failed"):
                evaluator._run_single_task(task)

            evaluator.state_manager.clean_up.assert_called_once_with(task)

    def test_cleanup_runs_after_verifier_exception_and_clears_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            evaluator = self._evaluator(Path(temp_dir))
            task = self._task()
            evaluator.state_manager.set_up.return_value = True
            evaluator.state_manager.clean_up.return_value = True
            evaluator.state_manager.set_verification_environment.side_effect = (
                lambda _: os.environ.__setitem__("MCP_MESSAGES", "set")
            )
            evaluator.task_manager.get_task_instruction.return_value = "instruction"
            evaluator.agent.execute_sync.return_value = {"success": True, "output": []}
            evaluator.task_manager.execute_task.side_effect = RuntimeError(
                "verify failed"
            )

            with self.assertRaisesRegex(RuntimeError, "verify failed"):
                evaluator._run_single_task(task)

            evaluator.state_manager.clean_up.assert_called_once_with(task)
            self.assertNotIn("MCP_MESSAGES", os.environ)

    def test_partial_setup_is_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            evaluator = self._evaluator(Path(temp_dir))
            task = self._task()
            evaluator.state_manager.set_up.return_value = False
            evaluator.state_manager.clean_up.return_value = True

            result = evaluator._run_single_task(task)

            self.assertIsInstance(result, TaskResult)
            self.assertFalse(result.success)
            evaluator.state_manager.clean_up.assert_called_once_with(task)

    def test_cleanup_runs_once_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            evaluator = self._evaluator(Path(temp_dir))
            task = self._task()
            expected = TaskResult(task_name=task.name, success=True)
            evaluator.state_manager.set_up.return_value = True
            evaluator.state_manager.clean_up.return_value = True
            evaluator.task_manager.get_task_instruction.return_value = "instruction"
            evaluator.agent.execute_sync.return_value = {"success": True, "output": []}
            evaluator.task_manager.execute_task.return_value = expected

            result = evaluator._run_single_task(task)

            self.assertIs(result, expected)
            evaluator.state_manager.clean_up.assert_called_once_with(task)
            self.assertGreaterEqual(result.task_execution_time, 0.0)
