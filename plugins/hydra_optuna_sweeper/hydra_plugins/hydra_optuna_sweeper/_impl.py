# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import functools
import logging
import sys
import warnings
from textwrap import dedent
from typing import (
    Any,
    Callable,
    Dict,
    List,
    MutableMapping,
    MutableSequence,
    Optional,
    Sequence,
    Tuple,
)

import optuna
from hydra._internal.deprecation_warning import deprecation_warning
from hydra.core.override_parser.overrides_parser import OverridesParser
from hydra.core.override_parser.types import (
    ChoiceSweep,
    IntervalSweep,
    Override,
    RangeSweep,
    Transformer,
)
from hydra.core.plugins import Plugins
from hydra.plugins.sweeper import Sweeper
from hydra.types import HydraContext, TaskFunction
from hydra.utils import get_method
from omegaconf import DictConfig, OmegaConf
from optuna.distributions import (
    BaseDistribution,
    CategoricalChoiceType,
    CategoricalDistribution,
    IntDistribution,
    FloatDistribution,
)
from optuna.trial import Trial

from .config import Direction, DistributionConfig, DistributionType

log = logging.getLogger(__name__)


def create_optuna_distribution_from_config(config: MutableMapping[str, Any]) -> BaseDistribution:
    kwargs = dict(config)
    if isinstance(config["type"], str):
        kwargs["type"] = DistributionType[config["type"]]
    param = DistributionConfig(**kwargs)
    if param.type == DistributionType.categorical:
        assert param.choices is not None
        return CategoricalDistribution(param.choices)
    if param.type == DistributionType.int:
        assert param.low is not None
        assert param.high is not None
        if param.log:
            return IntDistribution(int(param.low), int(param.high), log=True)
        step = int(param.step) if param.step is not None else 1
        return IntDistribution(int(param.low), int(param.high), step=step)
    if param.type == DistributionType.float:
        assert param.low is not None
        assert param.high is not None
        if param.log:
            return FloatDistribution(param.low, param.high, log=True)
        if param.step is not None:
            return FloatDistribution(param.low, param.high, step=param.step)
        return FloatDistribution(param.low, param.high)
    raise NotImplementedError(f"{param.type} is not supported by Optuna sweeper.")


def create_optuna_distribution_from_override(override: Override) -> Any:
    if not override.is_sweep_override():
        return override.get_value_element_as_str()

    value = override.value()
    choices: List[CategoricalChoiceType] = []
    if override.is_choice_sweep():
        assert isinstance(value, ChoiceSweep)
        for x in override.sweep_iterator(transformer=Transformer.encode):
            assert isinstance(x, (str, int, float, bool, type(None))), (
                f"A choice sweep expects str, int, float, bool, or None type. Got {type(x)}."
            )
            choices.append(x)
        return CategoricalDistribution(choices)

    if override.is_range_sweep():
        assert isinstance(value, RangeSweep)
        assert value.start is not None
        assert value.stop is not None
        if value.shuffle:
            for x in override.sweep_iterator(transformer=Transformer.encode):
                assert isinstance(x, (str, int, float, bool, type(None))), (
                    f"A choice sweep expects str, int, float, bool, or None type. Got {type(x)}."
                )
                choices.append(x)
            return CategoricalDistribution(choices)
        if (
            isinstance(value.start, float)
            or isinstance(value.stop, float)
            or isinstance(value.step, float)
        ):
            return FloatDistribution(value.start, value.stop, step=value.step)
        return IntDistribution(int(value.start), int(value.stop), step=int(value.step))

    if override.is_interval_sweep():
        assert isinstance(value, IntervalSweep)
        assert value.start is not None
        assert value.end is not None
        if isinstance(value.start, int) and isinstance(value.end, int):
            return IntDistribution(value.start, value.end, log="log" in value.tags)
        return FloatDistribution(value.start, value.end, log="log" in value.tags)

    raise NotImplementedError(f"{override} is not supported by Optuna sweeper.")


def create_params_from_overrides(
    arguments: List[str],
) -> Tuple[Dict[str, BaseDistribution], Dict[str, Any]]:
    parser = OverridesParser.create()
    parsed = parser.parse_overrides(arguments)
    search_space_distributions = dict()
    fixed_params = dict()

    for override in parsed:
        param_name = override.get_key_element()
        value = create_optuna_distribution_from_override(override)
        if isinstance(value, BaseDistribution):
            search_space_distributions[param_name] = value
        else:
            fixed_params[param_name] = value
    return search_space_distributions, fixed_params


class OptunaSweeperImpl(Sweeper):
    def __init__(
        self,
        sampler: Any,
        direction: Any,
        storage: Optional[Any],
        study_name: Optional[str],
        n_trials: int,
        n_jobs: int,
        max_failure_rate: float,
        search_space: Optional[DictConfig],
        custom_search_space: Optional[str],
        params: Optional[DictConfig],
    ) -> None:
        self.sampler = sampler
        self.direction = direction
        self.storage = storage
        self.study_name = study_name
        self.n_trials = n_trials
        self.n_jobs = n_jobs
        self.max_failure_rate = max_failure_rate
        assert self.max_failure_rate >= 0.0
        assert self.max_failure_rate <= 1.0
        self.custom_search_space_extender: Optional[Callable[[DictConfig, Trial], None]] = None
        if custom_search_space:
            self.custom_search_space_extender = get_method(custom_search_space)
        self.search_space = search_space
        self.params = params
        self.job_idx: int = 0
        self.search_space_distributions: Optional[Dict[str, BaseDistribution]] = None

    def _process_searchspace_config(self) -> None:
        url = "https://hydra.cc/docs/upgrades/1.1_to_1.2/changes_to_sweeper_config/"
        if self.params is None and self.search_space is None:
            self.params = OmegaConf.create({})
        elif self.search_space is not None:
            if self.params is not None:
                warnings.warn(
                    "Both hydra.sweeper.params and hydra.sweeper.search_space are configured."
                    "\nHydra will use hydra.sweeper.params for defining search space."
                    f"\n{url}"
                )
            else:
                deprecation_warning(
                    message=dedent(
                        f"""\
                        `hydra.sweeper.search_space` is deprecated and will be removed in the next major release.
                        Please configure with `hydra.sweeper.params`.
                        {url}
                        """
                    ),
                )
                self.search_space_distributions = {
                    str(x): create_optuna_distribution_from_config(y)
                    for x, y in self.search_space.items()
                }

    def setup(
        self,
        *,
        hydra_context: HydraContext,
        task_function: TaskFunction,
        config: DictConfig,
    ) -> None:
        self.job_idx = 0
        self.config = config
        self.hydra_context = hydra_context
        self.launcher = Plugins.instance().instantiate_launcher(
            config=config, hydra_context=hydra_context, task_function=task_function
        )
        self.sweep_dir = config.hydra.sweep.dir

    def _get_directions(self) -> List[str]:
        if isinstance(self.direction, MutableSequence):
            return [d.name if isinstance(d, Direction) else d for d in self.direction]
        elif isinstance(self.direction, str):
            return [self.direction]
        return [self.direction.name]

    def _configure_trials(
        self,
        trials: List[Trial],
        search_space_distributions: Dict[str, BaseDistribution],
        fixed_params: Dict[str, Any],
    ) -> Sequence[Sequence[str]]:
        overrides = []
        for trial in trials:
            for param_name, distribution in search_space_distributions.items():
                assert type(param_name) is str
                log.info(f"{param_name} is {distribution}")
                trial._suggest(param_name, distribution)
            for param_name, value in fixed_params.items():
                trial.set_user_attr(param_name, value)

            if self.custom_search_space_extender:
                assert self.config is not None
                self.custom_search_space_extender(self.config, trial)

            overlap = trial.params.keys() & trial.user_attrs
            if len(overlap):
                raise ValueError(
                    "Overlapping fixed parameters and search space parameters found!"
                    f"Overlapping parameters: {list(overlap)}"
                )
            params = dict(trial.params)
            params.update(fixed_params)

            overrides.append(tuple(f"{name}={val}" for name, val in params.items()))
        return overrides

    def _parse_sweeper_params_config(self) -> List[str]:
        if not self.params:
            return []

        return [f"{k!s}={v}" for k, v in self.params.items()]

    def _to_grid_sampler_choices(self, distribution: BaseDistribution) -> Any:
        if isinstance(distribution, CategoricalDistribution):
            return distribution.choices
        elif isinstance(distribution, IntDistribution):
            assert distribution.step is not None, (
                "`step` of IntUniformDistribution must be a positive integer."
            )
            n_items = (distribution.high - distribution.low) // distribution.step
            return [distribution.low + i * distribution.step for i in range(n_items)]
        elif isinstance(distribution, FloatDistribution):
            step = distribution.step
            assert step is not None, "`step` of FloatUniformDistribution must be a positive number."
            n_items = int((distribution.high - distribution.low) // step)
            return [distribution.low + i * step for i in range(n_items)]
        else:
            raise ValueError("GridSampler only supports discrete distributions.")

    def sweep(self, arguments: List[str]) -> None:
        assert self.config is not None
        assert self.launcher is not None
        assert self.hydra_context is not None
        assert self.job_idx is not None

        self._process_searchspace_config()
        params_conf = self._parse_sweeper_params_config()
        params_conf.extend(arguments)

        is_grid_sampler = (
            isinstance(self.sampler, functools.partial)
            and self.sampler.func == optuna.samplers.GridSampler
        )

        (
            override_search_space_distributions,
            fixed_params,
        ) = create_params_from_overrides(params_conf)

        search_space_distributions = dict()
        if self.search_space_distributions:
            search_space_distributions = self.search_space_distributions.copy()
        search_space_distributions.update(override_search_space_distributions)

        if is_grid_sampler:
            search_space_for_grid_sampler = {
                name: self._to_grid_sampler_choices(distribution)
                for name, distribution in search_space_distributions.items()
            }

            self.sampler = self.sampler(search_space_for_grid_sampler)
            n_trial = 1
            for v in search_space_for_grid_sampler.values():
                n_trial *= len(v)
            self.n_trials = min(self.n_trials, n_trial)
            log.info(f"Updating num of trials to {self.n_trials} due to using GridSampler.")

        # Remove fixed parameters from Optuna search space.
        for param_name in fixed_params:
            if param_name in search_space_distributions:
                del search_space_distributions[param_name]

        directions = self._get_directions()

        # if isinstance(self.sampler, functools.partial):
        #     if self.sampler.func == optuna.samplers.TPESampler:
        #         self.sampler =
        study = optuna.create_study(
            study_name=self.study_name,
            storage=self.storage,
            sampler=self.sampler,
            directions=directions,
            load_if_exists=True,
        )

        trials = study.get_trials(deepcopy=False)
        # completed_trials = study.get_trials(states=(optuna.trial.TrialState.COMPLETE,))
        completed_trials = list(
            [
                trial
                for trial in trials
                if trial.state
                not in {optuna.trial.TrialState.RUNNING, optuna.trial.TrialState.WAITING}
            ]
        )
        n_completed_trials = len(completed_trials)
        if len(trials) != n_completed_trials:
            print(
                f"Some trials are not completed, will keep {n_completed_trials} out of {len(trials)}"
            )
            assert self.study_name is not None
            optuna.delete_study(study_name=self.study_name, storage=self.storage)
            study = optuna.create_study(
                study_name=self.study_name,
                storage=self.storage,
                sampler=self.sampler,
                directions=directions,
                load_if_exists=True,
            )
            study.add_trials(completed_trials)

        log.info(f"Study name: {study.study_name}")
        log.info(f"Storage: {self.storage}")
        log.info(f"Sampler: {type(self.sampler).__name__}")
        log.info(f"Directions: {directions}")

        # completed_trials = study.get_trials(deepcopy=False, states=(optuna.trial.TrialState.COMPLETE,))
        # failed_trials = study.get_trials(deepcopy=False, states=(optuna.trial.TrialState.FAIL,))

        # log.info(f"Completed trials: {n_completed_trials}")
        # log.info(f"Failed trials: {len(failed_trials)}")
        # study.add_trials(completed_trials)
        # log.info(f"Added {n_completed_trials} completed trials to study.")

        batch_size = self.n_jobs
        n_trials_to_go = self.n_trials - n_completed_trials
        self.job_idx = n_completed_trials
        log.info(f"{n_trials_to_go=}, {self.job_idx=}, {n_completed_trials=}")

        while n_trials_to_go > 0:
            batch_size = min(n_trials_to_go, batch_size)

            trials = [study.ask() for _ in range(batch_size)]
            overrides = self._configure_trials(trials, search_space_distributions, fixed_params)

            returns = self.launcher.launch(overrides, initial_job_idx=self.job_idx)
            self.job_idx += len(returns)
            failures = []
            for trial, ret in zip(trials, returns):
                values: Optional[List[float]] = None
                state: optuna.trial.TrialState = optuna.trial.TrialState.COMPLETE
                try:
                    if len(directions) == 1:
                        try:
                            values = [float(ret.return_value)]
                        except (ValueError, TypeError):
                            raise ValueError(
                                f"Return value must be float-castable. Got '{ret.return_value}'."
                            ).with_traceback(sys.exc_info()[2])
                    else:
                        try:
                            values = [float(v) for v in ret.return_value]
                        except (ValueError, TypeError):
                            raise ValueError(
                                "Return value must be a list or tuple of float-castable values."
                                f" Got '{ret.return_value}'."
                            ).with_traceback(sys.exc_info()[2])
                        if len(values) != len(directions):
                            raise ValueError(
                                "The number of the values and the number of the objectives are"
                                f" mismatched. Expect {len(directions)}, but actually {len(values)}."
                            )

                    try:
                        study.tell(trial=trial, state=state, values=values)
                    except RuntimeError as e:
                        if (
                            is_grid_sampler
                            and "`Study.stop` is supposed to be invoked inside an objective function or a callback."
                            in str(e)
                        ):
                            pass
                        else:
                            raise e

                except Exception as e:
                    state = optuna.trial.TrialState.FAIL
                    study.tell(trial=trial, state=state, values=values)
                    log.warning(f"Failed experiment: {e}")
                    failures.append(e)

            # raise if too many failures
            if len(failures) / len(returns) > self.max_failure_rate:
                log.error(
                    f"Failed {failures} times out of {len(returns)} "
                    f"with max_failure_rate={self.max_failure_rate}."
                )
                assert len(failures) > 0
                for ret in returns:
                    ret.return_value  # delegate raising to JobReturn, with actual traceback

            n_trials_to_go -= batch_size

        results_to_serialize: Dict[str, Any]
        if len(directions) < 2:
            best_trial = study.best_trial
            results_to_serialize = {
                "name": "optuna",
                "best_params": best_trial.params,
                "best_value": best_trial.value,
            }
            log.info(f"Best parameters: {best_trial.params}")
            log.info(f"Best value: {best_trial.value}")
        else:
            best_trials = study.best_trials
            pareto_front = [{"params": t.params, "values": t.values} for t in best_trials]
            results_to_serialize = {
                "name": "optuna",
                "solutions": pareto_front,
            }
            log.info(f"Number of Pareto solutions: {len(best_trials)}")
            for t in best_trials:
                log.info(f"    Values: {t.values}, Params: {t.params}")
        OmegaConf.save(
            OmegaConf.create(results_to_serialize),
            f"{self.config.hydra.sweep.dir}/optimization_results.yaml",
        )
