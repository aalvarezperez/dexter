from typing import Any
from dexter.stats_func import trim_outliers, winsorize_outliers, check_multiple_proportion
from numpy import round, mean, sum, ndarray, sort
from dexter.utils import indent, print_nested_dict
from tabulate import tabulate
from pandas import DataFrame, concat
from itertools import product
from typing import Callable


def print_status_message(status_dict, exclude_keys=[]):
    print_nested_dict(status_dict, indent=0, exclude_keys=exclude_keys)
    print('')


class ExperimentChecker:
    def __init__(self, experiment):

        self._experiment = experiment
        self._crossover_mask = None
        self._log = {
            'group_balance': {
                'assumption': 'the group sizes have the pre-defined proportions',
                'info': 'if proportions deviate from the pre-defined ones, issues may exist in the randomisation.',
                'status': {
                    'checked': False,
                    'passed': None,
                    'handled': False
                    },
                'diagnostics': {
                    'groups': [],
                    'observed prop': [],
                    'expected prop': [],
                    'differences': [],
                    'tests results': {'tests': 'chi-square GOF', 'statistic': None, 'p-value': None}
                    }
                },
            'crossover': {
                'assumption': 'experiment units are exposed to a single variant only (no cross-over)',
                'info': 'if an experiment unit is exposed to multiple variants, '
                        'then the average treatment effect may be biased.',
                'status': {
                    'checked': False,
                    'passed': None,
                    'handled': False
                    },
                'diagnostics': {
                    'cross-over cases': None,
                    'percent of total': None
                    }
                },
            'outliers': {
                'assumption': 'depends on the definition of outliers that is used.',
                'info': 'careful handling of outliers will likely result in increased statistical power.',
                'status': {
                    'checked': False,
                    'passed': None,
                    'handled': False
                    },
                'diagnostics': {
                    'method': None,
                    'affected metrics': [],
                    'number of affected units': None,
                    'stats': None
                    }
                }
            }

    def get_log(self):
        return self._log

    def check_groups_balance(self):
        experiment = self._experiment
        data = experiment.data

        treatment = data.treatment
        expected_proportions = data.expected_proportions
        n_treatment = data[treatment].value_counts().sort_index()
        n_total = data.shape[0]
        observed_prop = round(n_treatment / n_total, 3).tolist()
        test_res = check_multiple_proportion(n_total, n_treatment, expected_proportions)
        differences = [round(e - o, 3) for e, o in zip(expected_proportions, observed_prop)]

        self._log['group_balance']['status']['checked'] = True
        self._log['group_balance']['status']['passed'] = test_res[1] > .05
        self._log['group_balance']['diagnostics']['groups'] = experiment.groups
        self._log['group_balance']['diagnostics']['observed prop'] = observed_prop
        self._log['group_balance']['diagnostics']['expected prop'] = expected_proportions
        self._log['group_balance']['diagnostics']['differences'] = differences
        self._log['group_balance']['diagnostics']['tests results']['statistic'] = test_res[0]
        self._log['group_balance']['diagnostics']['tests results']['p-value'] = test_res[1]

        print_status_message(self._log.get('group_balance'))

    def check_crossover(self):
        experiment = self._experiment
        experiment_unit = experiment.data.experiment_unit
        treatment = experiment.data.treatment

        data = experiment.data[[experiment_unit, treatment]]  # lighter df than full dataset
        user_treatment_df = data.drop_duplicates(subset=[experiment_unit, treatment])

        user_freq_series = user_treatment_df[experiment_unit].value_counts()
        crossed_over = user_freq_series > 1  # unique cases of cross-over

        absolute = crossed_over.sum()
        percent = '{}%'.format(round(crossed_over.mean() * 100))

        self._crossover_mask = crossed_over
        self._log['crossover']['status']['checked'] = True
        self._log['crossover']['status']['passed'] = absolute == 0
        self._log['crossover']['diagnostics']['cross-over cases'] = absolute
        self._log['crossover']['diagnostics']['percent of total'] = percent

        print_status_message(self._log.get('crossover'))

    def check_outliers(self, is_outlier, metrics, func: Callable):

        data = self._experiment.data

        func = [func] if not isinstance(func, list) else func

        aggr_df = data[metrics].groupby(is_outlier).agg(func)

        aggr_df.rename(index={True: 'Outliers', False: 'Regulars'}, inplace=True)

        aggr_df = aggr_df.round(3)

        deltas = ((aggr_df.loc['Outliers', :] / aggr_df.loc['Regulars', :]) - 1) * 100
        deltas = [f'({x:.3g}%)' for x in deltas]

        cols = product(metrics, [f.__name__ for f in func])

        delta_df = DataFrame(
            [deltas],
            index=['(delta)'],
            columns=[x for x in cols]
            )

        aggr_df = concat([aggr_df, delta_df])

        aggr_df.index.name = ''

        self._log['outliers']['status']['checked'] = True
        self._log['outliers']['diagnostics']['stats'] = aggr_df.to_dict()
        self._crossover_mask = is_outlier

        print_status_message(self._log.get('outliers'), exclude_keys=['stats'])

        headers = [aggr_df.index.name] + list(map('\n '.join, aggr_df.columns.tolist()))

        print(
            'Stats:\n',
            indent(tabulate(aggr_df, headers=headers, showindex=True, floatfmt='.3f', tablefmt='simple'), 1),
            '\n'
            )

        print('The check_outliers() method will not affect the diagnostics for this assumption. '
              'Only handling it will.' + '\n')

    def get_status(self, detailed=False):
        if detailed:
            print_status_message(self._log)
        else:
            print_status_message(self._log, exclude_keys=['assumption', 'info', 'diagnostics', 'stats'])

    def handle_crossover(self, threshold=.01, force=False):

        if self._log['crossover']['status']['checked'] is False:
            self.check_crossover()

        print('• Handling cross-overs...')

        if self._crossover_mask is None:
            print(indent('Nothing to take care of. Have you ran the check for this assumption first?'+'\n'))
            return

        if sum(self._crossover_mask == 0):
            print(indent('There are no cross-over cases to handle. You are good to go.'+'\n'))
            return

        if mean(self._crossover_mask) > threshold:
            if not force:
                raise Exception(f'More than {round(threshold * 100, 3)}% units were exposed to multiple variants'
                                f'It is not recommended to proceed without better understanding why this is the case.')

        affected = sum(self._crossover_mask)

        print(f'{affected} units were removed from the working dataset.')

        self._experiment.data.data = self._experiment.data.loc[~self._crossover_mask]

        self._log['crossover']['status']['handled'] = True

    def handle_outliers(self, metrics, method, is_outlier=None, func=None):
        experiment = self._experiment

        _default_metrics = [*experiment.data.success_metric, *experiment.data.learning_metrics]
        _default_func = mean

        metrics = _default_metrics if metrics is None else metrics
        func = _default_func if func is None else func

        if is_outlier is None and self._log['outliers']['status']['checked'] is False:
            raise Exception('Provide a boolean mask that identifies outliers.')

        if self._log['outliers']['status']['checked'] is False:
            self.check_outliers(is_outlier=is_outlier, metrics=metrics, func=func)

        print('• Handling outliers...')

        if metrics is None:
            print(indent('All success and learning metrics are affected by default. See the "metrics" argument.'))

        # choose a method to remove outliers
        method_dict = {
            'trim': trim_outliers,
            'winsorize': winsorize_outliers
            }

        outlier_fun = method_dict[method]

        experiment.data.data = outlier_fun(dataframe=self._experiment.data, outlier_mask=is_outlier, metrics=metrics)

        total_affected = is_outlier.sum()
        percent_affected = is_outlier.mean()

        self._log['outliers']['status']['handled'] = True
        self._log['outliers']['diagnostics']['method'] = method
        self._log['outliers']['diagnostics']['affected metrics'] = metrics
        self._log['outliers']['diagnostics']['number of affected units'] = total_affected

        print(indent('{} experiment units were affected: {}% of the total sample.\n'
                     .format(total_affected, round(percent_affected * 100, 3))))