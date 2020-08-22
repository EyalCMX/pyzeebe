import concurrent.futures
import socket
from typing import Tuple, List, Callable, Generator

from pyz.base_types.base import ZeebeBase
from pyz.decorators.base_zeebe_decorator import BaseZeebeDecorator
from pyz.decorators.task_decorator import TaskDecorator
from pyz.exceptions import TaskNotFoundException
from pyz.task.task import Task
from pyz.task.task_context import TaskContext
from pyz.task.task_status_setter import TaskStatusSetter


class ZeebeWorker(ZeebeBase, BaseZeebeDecorator):
    def __init__(self, name: str = None, request_timeout: int = 0, hostname: str = None, port: int = None,
                 before: List[TaskDecorator] = None, after: List[TaskDecorator] = None):
        ZeebeBase.__init__(self, hostname=hostname, port=port)
        BaseZeebeDecorator.__init__(self, before=before, after=after)
        self.name = name or socket.gethostname()
        self.request_timeout = request_timeout
        self.tasks: List[Task] = []

    def work(self) -> None:
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(self.tasks))
        for task in self.tasks:
            executor.submit(self.handle_task, task=task)
        executor.shutdown(wait=True)

    def handle_task(self, task: Task) -> None:
        while self.connected or self.retrying_connection:
            if self.retrying_connection:
                continue

            executor = concurrent.futures.ThreadPoolExecutor()
            for task_context in self._generate_tasks(task):
                executor.submit(task.handler, context=task_context)
            executor.shutdown(wait=False)  # Do not wait for tasks to finish

    def _generate_tasks(self, task: Task) -> Generator[TaskContext, None, None]:
        return self.zeebe_client.activate_jobs(task_type=task.type, worker=self.name, timeout=task.timeout,
                                               max_jobs_to_activate=task.max_jobs_to_activate,
                                               variables_to_fetch=task.variables_to_fetch,
                                               request_timeout=self.request_timeout)

    def remove_task(self, task_type: str) -> Task:
        task, index = self.get_task(task_type)
        return self.tasks.pop(index)

    def get_task(self, task_type: str) -> Tuple[Task, int]:
        for index, task in enumerate(self.tasks):
            if self._is_task_of_type(task, task_type):
                return task, index
        raise TaskNotFoundException(f"Could not find task {task_type}")

    def add_task(self, task: Task) -> None:
        task.handler = self.create_handler(task)
        self.tasks.append(task)

    def create_handler(self, task: Task):
        before_decorators_runner = self._create_decorator_handler(self._merge_before_decorators(task))
        after_decorators_runner = self._create_decorator_handler(self._merge_after_decorators(task))

        def task_handler(context: TaskContext):
            try:
                before_output = before_decorators_runner(context)
                task_output = task.inner_function(**before_output.variables)
                before_output.variables = task_output
                after_output = after_decorators_runner(before_output)
                self.zeebe_client.complete_job(job_key=after_output.key, variables=after_output.variables)
            except Exception as e:
                task.exception_handler(e, context, TaskStatusSetter(context, self.zeebe_client))

        return task_handler

    @staticmethod
    def _create_decorator_handler(decorators: List[TaskDecorator]) -> Callable[[TaskContext], TaskContext]:
        def decorator_runner(context: TaskContext):
            for decorator in decorators:
                context = decorator(context)
            return context

        return decorator_runner

    def _merge_before_decorators(self, decorator_instance: Task) -> List[TaskDecorator]:
        decorators = decorator_instance._before.copy()
        decorators.extend(self._before)
        return decorators

    def _merge_after_decorators(self, decorator_instance: Task):
        decorators = decorator_instance._after.copy()
        decorators.extend(self._after)
        return decorators

    @staticmethod
    def _is_task_of_type(task: Task, task_type: str) -> bool:
        return task.type == task_type
