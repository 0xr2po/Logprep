# pylint: disable=missing-docstring
# pylint: disable=protected-access
# pylint: disable=attribute-defined-outside-init
import re
from copy import deepcopy
from logging import DEBUG, WARNING, getLogger
from multiprocessing import Lock, active_children
from unittest import mock

import pytest
import requests
from _pytest.outcomes import fail
from _pytest.python_api import raises

from logprep.abc.input import (
    CriticalInputError,
    FatalInputError,
    SourceDisconnectedError,
    WarningInputError,
)
from logprep.abc.output import CriticalOutputError, FatalOutputError, Output, WarningOutputError
from logprep.abc.processor import Processor
from logprep.factory import Factory
from logprep.framework.pipeline import (
    MultiprocessingPipeline,
    MustProvideALogHandlerError,
    MustProvideAnMPLogHandlerError,
    Pipeline,
    SharedCounter,
)
from logprep.metrics.metric import MetricTargets
from logprep.processor.base.exceptions import ProcessingWarning, ProcessingWarningCollection
from logprep.processor.deleter.rule import DeleterRule
from logprep.util.getter import GetterFactory
from logprep.util.multiprocessing_log_handler import MultiprocessingLogHandler

original_create = Factory.create


class ConfigurationForTests:
    logprep_config = {
        "version": 1,
        "timeout": 0.001,
        "print_processed_period": 600,
        "input": {"dummy": {"type": "dummy_input", "documents": [{"test": "empty"}]}},
        "output": {"dummy": {"type": "dummy_output"}},
        "pipeline": [{"mock_processor1": {"proc": "conf"}}, {"mock_processor2": {"proc": "conf"}}],
        "metrics": {"period": 300, "enabled": False},
    }
    log_handler = MultiprocessingLogHandler(WARNING)
    lock = Lock()
    shared_dict = {}
    metric_targets = MetricTargets(file_target=getLogger("Mock"), prometheus_target=None)
    counter = SharedCounter()


class ProcessorWarningMockError(ProcessingWarning):
    def __init__(self):
        super().__init__("ProcessorWarningMockError")


@mock.patch("logprep.factory.Factory.create")
class TestPipeline(ConfigurationForTests):
    def setup_method(self):
        self._check_failed_stored = None

        self.pipeline = Pipeline(
            pipeline_index=1,
            config=self.logprep_config,
            counter=self.counter,
            log_handler=self.log_handler,
            lock=self.lock,
            shared_dict=self.shared_dict,
            used_server_ports=mock.MagicMock(),
            metric_targets=self.metric_targets,
        )

    def test_fails_if_log_handler_is_not_of_type_loghandler(self, _):
        for not_a_log_handler in [123, 45.67, TestPipeline()]:
            with raises(MustProvideALogHandlerError):
                _ = Pipeline(
                    pipeline_index=1,
                    config=self.logprep_config,
                    counter=self.counter,
                    log_handler=not_a_log_handler,
                    lock=self.lock,
                    shared_dict=self.shared_dict,
                    used_server_ports=mock.MagicMock(),
                    metric_targets=self.metric_targets,
                )

    def test_pipeline_property_returns_pipeline(self, mock_create):
        assert len(self.pipeline._pipeline) == 2
        assert mock_create.call_count == 4  # 2 processors, 1 input, 1 output

    def test_setup_calls_setup_on_pipeline_processors(self, _):
        self.pipeline._setup()
        assert len(self.pipeline._pipeline) == 2
        for processor in self.pipeline._pipeline:
            processor.setup.assert_called()

    def test_shut_down_calls_shut_down_on_pipeline_processors(self, _):
        self.pipeline._setup()
        processors = list(self.pipeline._pipeline)
        self.pipeline._shut_down()
        for processor in processors:
            processor.shut_down.assert_called()

    def test_setup_calls_setup_on_input_and_output(self, _):
        self.pipeline._setup()
        self.pipeline._input.setup.assert_called()
        for _, output in self.pipeline._output.items():
            output.setup.assert_called()

    def test_passes_timeout_parameter_to_inputs_get_next(self, _):
        self.pipeline._setup()
        self.pipeline._input.get_next.return_value = ({}, {})
        self.pipeline.process_pipeline()
        timeout = self.logprep_config.get("timeout")
        self.pipeline._input.get_next.assert_called_with(timeout)

    def test_empty_documents_are_not_forwarded_to_other_processors(self, _):
        input_data = [{"do_not_delete": "1"}, {"delete_me": "2"}, {"do_not_delete": "3"}]
        connector_config = {"dummy": {"type": "dummy_input", "documents": input_data}}
        input_connector = original_create(connector_config, mock.MagicMock())
        self.pipeline._input = input_connector
        self.pipeline._output = {
            "dummy": original_create({"dummy": {"type": "dummy_output"}}, mock.MagicMock()),
        }
        deleter_config = {
            "deleter processor": {
                "type": "deleter",
                "specific_rules": ["tests/testdata/unit/deleter/rules/specific"],
                "generic_rules": ["tests/testdata/unit/deleter/rules/generic"],
            }
        }
        deleter_processor = original_create(deleter_config, mock.MagicMock())
        deleter_rule = DeleterRule._create_from_dict({"filter": "delete_me", "delete": True})
        deleter_processor._specific_tree.add_rule(deleter_rule)
        self.pipeline._pipeline = [mock.MagicMock(), deleter_processor, mock.MagicMock()]
        self.pipeline._logger.setLevel(DEBUG)
        while self.pipeline._input._documents:
            self.pipeline.process_pipeline()
        assert len(input_data) == 0, "all events were processed"
        assert self.pipeline._pipeline[0].process.call_count == 3, "called for all events"
        assert self.pipeline._pipeline[2].process.call_count == 2, "not called for deleted event"
        assert {"delete_me": "2"} not in self.pipeline._output["dummy"].events
        assert len(self.pipeline._output["dummy"].events) == 2

    def test_not_empty_documents_are_stored_in_the_output(self, _):
        self.pipeline._setup()
        self.pipeline._input.get_next.return_value = ({"message": "test"}, None)
        self.pipeline._store_event = mock.MagicMock()
        self.pipeline.process_pipeline()
        assert self.pipeline._store_event.call_count == 1

    def test_empty_documents_are_not_stored_in_the_output(self, _):
        self.pipeline.process_event = lambda x: x.clear()
        self.pipeline._setup()
        self.pipeline._input.get_next.return_value = ({"message": "test"}, None)
        self.pipeline._store_event = mock.MagicMock()
        self.pipeline.process_pipeline()
        assert self.pipeline._store_event.call_count == 0

    def test_setup_calls_setup_on_input(self, _):
        self.pipeline._setup()
        self.pipeline._input.setup.assert_called()

    def test_setup_calls_setup_on_output(self, _):
        self.pipeline._setup()
        for _, output in self.pipeline._output.items():
            output.setup.assert_called()

    def test_shut_down_calls_shut_down_on_input(self, _):
        self.pipeline._setup()
        self.pipeline._shut_down()
        self.pipeline._input.shut_down.assert_called()

    def test_shut_down_calls_shut_down_on_output(self, _):
        self.pipeline._setup()
        self.pipeline._shut_down()
        for _, output in self.pipeline._output.items():
            output.shut_down.assert_called()

    @mock.patch("logging.Logger.warning")
    def test_logs_source_disconnected_error_as_warning(self, mock_warning, _):
        self.pipeline._setup()
        self.pipeline._input.get_next.side_effect = SourceDisconnectedError
        self.pipeline.run()
        mock_warning.assert_called()
        assert re.search(
            r"Lost or failed to establish connection to ", mock_warning.call_args[0][0]
        )

    def test_all_events_provided_by_input_arrive_at_output(self, _):
        self.pipeline._setup()
        self.pipeline._setup = mock.MagicMock()
        input_data = [{"test": "1"}, {"test": "2"}, {"test": "3"}]
        expected_output_data = deepcopy(input_data)
        connector_config = {"type": "dummy_input", "documents": input_data}
        self.pipeline._input = original_create({"dummy": connector_config}, mock.MagicMock())
        self.pipeline._output = {
            "dummy": original_create({"dummy": {"type": "dummy_output"}}, mock.MagicMock())
        }
        self.pipeline.run()
        assert self.pipeline._output["dummy"].events == expected_output_data

    def test_enable_iteration_sets_iterate_to_true_stop_to_false(self, _):
        assert not self.pipeline._iterate()

        self.pipeline._enable_iteration()
        assert self.pipeline._iterate()

        self.pipeline.stop()
        assert not self.pipeline._iterate()

    @mock.patch("logging.Logger.error")
    def test_critical_input_error_is_logged_and_stored_as_failed(self, mock_error, _):
        def raise_critical_input_error(event):
            raise CriticalInputError("An error message", event)

        self.pipeline._setup()
        self.pipeline._input.get_next.side_effect = raise_critical_input_error
        self.pipeline._output = {
            "dummy": original_create({"dummy": {"type": "dummy_output"}}, mock.MagicMock())
        }
        self.pipeline.process_pipeline()
        assert len(self.pipeline._output["dummy"].events) == 0
        mock_error.assert_called()
        assert re.search(
            "A critical error occurred for input .*: An error message", mock_error.call_args[0][0]
        )
        assert len(self.pipeline._output["dummy"].failed_events) == 1

    @mock.patch("logging.Logger.error")
    def test_critical_output_error_is_logged_and_stored_as_failed(self, mock_error, _):
        def raise_critical_output_error(event):
            raise CriticalOutputError("An error message", event)

        self.pipeline._setup()
        self.pipeline._input.get_next.return_value = ({"order": 1}, None)
        self.pipeline._output["dummy"].store.side_effect = raise_critical_output_error
        self.pipeline.process_pipeline()
        self.pipeline._output["dummy"].store_failed.assert_called()
        mock_error.assert_called()
        assert re.search(
            r"A critical error occurred for output .*: An error message", mock_error.call_args[0][0]
        )

    @mock.patch("logging.Logger.warning")
    def test_input_warning_error_is_logged_but_processing_continues(self, mock_warning, _):
        self.pipeline._setup()
        self.pipeline._input.metrics = mock.MagicMock()
        self.pipeline._input.metrics.number_of_warnings = 0
        self.pipeline._input.get_next.return_value = ({"order": 1}, None)
        self.pipeline.process_pipeline()
        self.pipeline._input.get_next.side_effect = WarningInputError
        self.pipeline.process_pipeline()
        self.pipeline._input.get_next.side_effect = None
        self.pipeline.process_pipeline()
        assert self.pipeline._input.get_next.call_count == 3
        assert mock_warning.call_count == 1
        assert self.pipeline._output["dummy"].store.call_count == 2
        assert self.pipeline._input.metrics.number_of_warnings == 1

    @mock.patch("logging.Logger.warning")
    def test_output_warning_error_is_logged_but_processing_continues(self, mock_warning, _):
        self.pipeline._setup()
        self.pipeline._input.get_next.return_value = ({"order": 1}, None)
        self.pipeline._output["dummy"].metrics = mock.MagicMock()
        self.pipeline._output["dummy"].metrics.number_of_warnings = 0
        self.pipeline.process_pipeline()
        self.pipeline._output["dummy"].store.side_effect = WarningOutputError
        self.pipeline.process_pipeline()
        self.pipeline._output["dummy"].store.side_effect = None
        self.pipeline.process_pipeline()
        assert self.pipeline._input.get_next.call_count == 3
        assert mock_warning.call_count == 1
        assert self.pipeline._output["dummy"].store.call_count == 3
        assert self.pipeline._output["dummy"].metrics.number_of_warnings == 1

    @mock.patch("logging.Logger.warning")
    def test_processor_warning_error_is_logged_but_processing_continues(self, mock_warning, _):
        self.pipeline._setup()
        self.pipeline._input.get_next.return_value = ({"message": "test"}, None)
        self.pipeline._pipeline[1].process.side_effect = ProcessorWarningMockError
        self.pipeline.process_pipeline()
        self.pipeline._input.get_next.return_value = ({"message": "test"}, None)
        self.pipeline.process_pipeline()
        mock_warning.assert_called()
        assert (
            "ProcessorWarningMockError" in mock_warning.call_args[0][0]
        ), "the log message was written"
        assert self.pipeline._output["dummy"].store.call_count == 2, "all events are processed"

    @mock.patch("logging.Logger.warning")
    def test_processor_warning_error_is_logged_for_processingwarningcollection(
        self, mock_warning, _
    ):
        class ProcessingWarningMockCollection(ProcessingWarningCollection):
            def __init__(self):
                super().__init__(
                    "name",
                    "message",
                    [ProcessingWarning("warning1"), ProcessingWarning("warning2")],
                )

        self.pipeline._setup()
        self.pipeline._input.get_next.return_value = ({"message": "test"}, None)
        self.pipeline._pipeline[0].process.side_effect = ProcessingWarningMockCollection
        self.pipeline.process_pipeline()
        assert mock_warning.call_count == 4, "called 2 times for 2 processors in pipeline"
        assert self.pipeline._output["dummy"].store.call_count == 1, "the event is processed"

    @mock.patch("logging.Logger.error")
    def test_processor_critical_error_is_logged_event_is_stored_in_error_output(
        self, mock_error, _
    ):
        self.pipeline._setup()
        self.pipeline._input.get_next.return_value = ({"message": "test"}, None)
        self.pipeline._pipeline[1].process.side_effect = Exception
        self.pipeline.process_pipeline()
        self.pipeline._input.get_next.return_value = ({"message": "test"}, None)
        self.pipeline.process_pipeline()
        assert self.pipeline._input.get_next.call_count == 2, "2 events gone into processing"
        assert mock_error.call_count == 2, "two errors occured"
        assert (
            "A critical error occurred for processor" in mock_error.call_args[0][0]
        ), "the log error message was written"
        assert self.pipeline._output["dummy"].store.call_count == 0, "no event in output"
        assert (
            self.pipeline._output["dummy"].store_failed.call_count == 2
        ), "errored events are gone to connector error output handler"

    @mock.patch("logging.Logger.error")
    def test_critical_input_error_is_logged_error_is_stored_in_failed_events(self, mock_error, _):
        def raise_critical(args):
            raise CriticalInputError("mock input error", args)

        self.pipeline._setup()
        self.pipeline._input.metrics = mock.MagicMock()
        self.pipeline._input.metrics.number_of_errors = 0
        self.pipeline._input.get_next.return_value = ({"message": "test"}, None)
        self.pipeline._input.get_next.side_effect = raise_critical
        self.pipeline.process_pipeline()
        self.pipeline._input.get_next.assert_called()
        mock_error.assert_called()
        assert re.search(
            r"A critical error occurred for input .*: mock input error", mock_error.call_args[0][0]
        ), "error message is logged"
        assert self.pipeline._output["dummy"].store_failed.call_count == 1, "one error is stored"
        assert self.pipeline._output["dummy"].store.call_count == 0, "no event is stored"
        assert self.pipeline._input.metrics.number_of_errors == 1, "counts error metric"

    @mock.patch("logging.Logger.warning")
    def test_input_warning_is_logged(self, mock_warning, _):
        def raise_warning(args):
            raise WarningInputError("mock input warning", args)

        self.pipeline._setup()
        self.pipeline._input.get_next.side_effect = raise_warning
        self.pipeline.process_pipeline()
        self.pipeline._input.get_next.assert_called()
        mock_warning.assert_called()
        assert re.search(
            r"An error occurred for input .*:", mock_warning.call_args[0][0]
        ), "error message is logged"

    @mock.patch("logging.Logger.error")
    def test_critical_output_error_is_logged(self, mock_error, _):
        def raise_critical(args):
            raise CriticalOutputError("mock output error", args)

        self.pipeline._setup()
        self.pipeline._input.get_next.return_value = ({"test": "message"}, None)
        self.pipeline._output["dummy"].metrics = mock.MagicMock()
        self.pipeline._output["dummy"].metrics.number_of_errors = 0
        self.pipeline._output["dummy"].store.side_effect = raise_critical
        self.pipeline.process_pipeline()
        self.pipeline._output["dummy"].store_failed.assert_called()
        mock_error.assert_called()
        assert re.search(
            r"A critical error occurred for output .*: mock output error",
            mock_error.call_args[0][0],
        ), "error message is logged"
        assert self.pipeline._output["dummy"].metrics.number_of_errors == 1, "counts error metric"

    @mock.patch("logging.Logger.warning")
    def test_warning_output_error_is_logged(self, mock_warning, _):
        self.pipeline._setup()
        self.pipeline._input.get_next.return_value = ({"some": "event"}, None)
        self.pipeline._output["dummy"].store.side_effect = WarningOutputError("mock output warning")
        self.pipeline.process_pipeline()
        self.pipeline._output["dummy"].store.assert_called()
        mock_warning.assert_called()
        assert mock_warning.call_args[0][0].startswith(
            "An error occurred for output"
        ), "error message is logged"

    @mock.patch("logprep.framework.pipeline.Pipeline._shut_down")
    @mock.patch("logging.Logger.error")
    def test_processor_fatal_input_error_is_logged_pipeline_is_rebuilt(
        self, mock_error, mock_shut_down, _
    ):
        self.pipeline._setup()
        self.pipeline._input.get_next.side_effect = FatalInputError
        self.pipeline.run()
        self.pipeline._input.get_next.assert_called()
        mock_error.assert_called()
        assert re.search(r"Input .* failed:", mock_error.call_args[0][0]), "error message is logged"
        mock_shut_down.assert_called()

    @mock.patch("logprep.framework.pipeline.Pipeline._shut_down")
    @mock.patch("logging.Logger.error")
    def test_processor_fatal_output_error_is_logged_pipeline_is_rebuilt(
        self, mock_error, mock_shut_down, _
    ):
        self.pipeline._setup()
        self.pipeline._input.get_next.return_value = ({"some": "event"}, None)
        self.pipeline._output["dummy"].store.side_effect = FatalOutputError
        self.pipeline.run()
        self.pipeline._output["dummy"].store.assert_called()
        mock_error.assert_called()
        assert re.search("Output .* failed:", mock_error.call_args[0][0]), "error message is logged"
        mock_shut_down.assert_called()

    @mock.patch("logging.Logger.error")
    def test_processor_fatal_output_error_in_setup_is_logged_pipeline_is_rebuilt(
        self, mock_error, _
    ):
        self.pipeline._output = {"dummy": mock.MagicMock()}
        self.pipeline._output["dummy"].setup.side_effect = FatalOutputError
        self.pipeline.run()
        mock_error.assert_called()
        assert re.search("Output .* failed:", mock_error.call_args[0][0]), "error message is logged"

    def test_extra_data_tuple_is_passed_to_store_custom(self, _):
        self.pipeline._setup()
        self.pipeline._input.get_next.return_value = ({"some": "event"}, None)
        processor_with_extra_data = mock.MagicMock()
        processor_with_extra_data.process = mock.MagicMock()
        processor_with_extra_data.process.return_value = ([{"foo": "bar"}], ({"dummy": "target"},))
        self.pipeline._pipeline = [mock.MagicMock(), processor_with_extra_data, mock.MagicMock()]
        self.pipeline.process_pipeline()
        assert self.pipeline._input.get_next.call_count == 1
        assert self.pipeline._output["dummy"].store_custom.call_count == 1
        self.pipeline._output["dummy"].store_custom.assert_called_with({"foo": "bar"}, "target")

    def test_store_custom_calls_all_defined_outputs(self, _):
        self.pipeline._output.update({"dummy1": mock.MagicMock()})
        self.pipeline._setup()
        self.pipeline._input.get_next.return_value = ({"some": "event"}, None)
        processor_with_extra_data = mock.MagicMock()
        processor_with_extra_data.process = mock.MagicMock()
        processor_with_extra_data.process.return_value = (
            [{"foo": "bar"}],
            ({"dummy": "target"}, {"dummy1": "second_target"}),
        )
        self.pipeline._pipeline = [mock.MagicMock(), processor_with_extra_data, mock.MagicMock()]
        self.pipeline.process_pipeline()
        assert self.pipeline._input.get_next.call_count == 1
        assert self.pipeline._output["dummy"].store_custom.call_count == 1
        assert self.pipeline._output["dummy1"].store_custom.call_count == 1
        self.pipeline._output["dummy"].store_custom.assert_called_with({"foo": "bar"}, "target")
        self.pipeline._output["dummy1"].store_custom.assert_called_with(
            {"foo": "bar"}, "second_target"
        )

    def test_extra_data_list_is_passed_to_store_custom(self, _):
        self.pipeline._setup()
        self.pipeline._input.get_next.return_value = ({"some": "event"}, None)
        processor_with_extra_data = mock.MagicMock()
        processor_with_extra_data.process = mock.MagicMock()
        processor_with_extra_data.process.return_value = [
            ([{"foo": "bar"}], ({"dummy": "target"},))
        ]
        self.pipeline._pipeline = [mock.MagicMock(), processor_with_extra_data, mock.MagicMock()]
        self.pipeline.process_pipeline()
        assert self.pipeline._input.get_next.call_count == 1
        assert self.pipeline._output["dummy"].store_custom.call_count == 1
        self.pipeline._output["dummy"].store_custom.assert_called_with({"foo": "bar"}, "target")

    def test_pipeline_metrics_number_of_events_counts_process_event_method_counts(
        self,
        _,
    ):
        self.pipeline.process_event({"sample_event": "one"})
        self.pipeline.process_event({"sample_event": "two"})
        assert self.pipeline.metrics.number_of_processed_events == 2

    def test_pipeline_metrics_number_of_warnings_counts_warnings_of_all_processor_metrics(
        self,
        _,
    ):
        mock_metrics_one = Processor.ProcessorMetrics(
            labels={"any": "label"},
            generic_rule_tree=mock.MagicMock(),
            specific_rule_tree=mock.MagicMock(),
        )
        mock_metrics_one.number_of_warnings = 1
        mock_metrics_two = Processor.ProcessorMetrics(
            labels={"any_other": "label"},
            generic_rule_tree=mock.MagicMock(),
            specific_rule_tree=mock.MagicMock(),
        )
        mock_metrics_two.number_of_warnings = 1
        self.pipeline._setup()
        self.pipeline.metrics.pipeline = [mock_metrics_one, mock_metrics_two]
        assert self.pipeline.metrics.sum_of_processor_warnings == 2

    def test_pipeline_metrics_number_of_errors_counts_errors_of_all_processor_metrics(
        self,
        _,
    ):
        mock_metrics_one = Processor.ProcessorMetrics(
            labels={"any": "label"},
            generic_rule_tree=mock.MagicMock(),
            specific_rule_tree=mock.MagicMock(),
        )
        mock_metrics_one.number_of_errors = 1
        mock_metrics_two = Processor.ProcessorMetrics(
            labels={"any_other": "label"},
            generic_rule_tree=mock.MagicMock(),
            specific_rule_tree=mock.MagicMock(),
        )
        mock_metrics_two.number_of_errors = 1
        self.pipeline._setup()
        self.pipeline.metrics.pipeline = [mock_metrics_one, mock_metrics_two]
        assert self.pipeline.metrics.sum_of_processor_errors == 2

    def test_setup_adds_versions_information_to_input_connector_config(self, mock_create):
        self.pipeline._setup()
        called_input_config = mock_create.call_args_list[1][0][0]["dummy"]
        assert "version_information" in called_input_config, "ensure version_information is added"
        assert "logprep" in called_input_config.get("version_information"), "ensure logprep key"
        assert "configuration" in called_input_config.get("version_information"), "ensure config"
        assert called_input_config.get("version_information").get("logprep"), "ensure values"
        assert called_input_config.get("version_information").get("configuration"), "ensure values"

    def test_setup_connects_output_with_input(self, _):
        self.pipeline._setup()
        assert self.pipeline._output["dummy"].input_connector == self.pipeline._input

    def test_pipeline_does_not_call_batch_finished_callback_if_output_store_does_not_return_true(
        self, _
    ):
        self.pipeline._setup()
        self.pipeline._input.get_next.return_value = ({"some": "event"}, None)
        self.pipeline._input.batch_finished_callback = mock.MagicMock()
        self.pipeline._output["dummy"].store = mock.MagicMock()
        self.pipeline._output["dummy"].store.return_value = None
        self.pipeline.process_pipeline()
        self.pipeline._input.batch_finished_callback.assert_not_called()

    def test_retrieve_and_process_data_calls_store_failed_for_non_critical_error_message(self, _):
        self.pipeline._setup()
        self.pipeline._input.get_next.return_value = ({"some": "event"}, "This is non critical")
        self.pipeline.process_pipeline()
        self.pipeline._output["dummy"].store_failed.assert_called_with(
            "This is non critical", {"some": "event"}, None
        )

    def test_http_input_registers_to_shard_dict(self, _):
        self.pipeline._setup()
        self.pipeline._input.server.config.port = 9000
        self.pipeline._used_server_ports = {}
        self.pipeline._setup()
        assert 9000 in self.pipeline._used_server_ports

    def test_http_input_registers_increased_port_to_shard_dict(self, _):
        self.pipeline._setup()
        self.pipeline._input.server.config.port = 9000
        self.pipeline._used_server_ports = {9000: "other_process_name"}
        self.pipeline._setup()
        assert 9001 in self.pipeline._used_server_ports

    def test_http_input_removes_port_from_shard_dict_on_shut_down(self, _):
        self.pipeline._setup()
        self.pipeline._input.server.config.port = 9000
        self.pipeline._used_server_ports = {}
        self.pipeline._setup()
        assert 9000 in self.pipeline._used_server_ports
        self.pipeline._shut_down()
        assert 9000 not in self.pipeline._used_server_ports

    def test_http_input_registers_increased_port_to_shard_dict_after_shut_down(self, _):
        self.pipeline._setup()
        self.pipeline._input.server.config.port = 9000
        self.pipeline._used_server_ports = {9000: "other_process_name"}
        self.pipeline._setup()
        assert 9001 in self.pipeline._used_server_ports
        self.pipeline._shut_down()
        assert 9001 not in self.pipeline._used_server_ports
        self.pipeline._setup()
        assert 9001 in self.pipeline._used_server_ports

    def test_shut_down_drains_input_queues(self, _):
        self.pipeline._setup()
        input_config = {
            "testinput": {
                "type": "http_input",
                "uvicorn_config": {
                    "host": "127.0.0.1",
                    "port": 9000,
                    "ssl_certfile": "tests/testdata/acceptance/http_input/cert.crt",
                    "ssl_keyfile": "tests/testdata/acceptance/http_input/cert.key",
                },
                "endpoints": {"/json": "json", "/jsonl": "jsonl", "/plaintext": "plaintext"},
            }
        }
        self.pipeline._input = original_create(input_config, mock.MagicMock())
        self.pipeline._input.messages.put({"message": "test message"})
        assert self.pipeline._input.messages.qsize() == 1
        self.pipeline._shut_down()
        assert self.pipeline._input.messages.qsize() == 0

    def test_pipeline_raises_http_error_from_factory_create(self, mock_create):
        mock_create.side_effect = requests.HTTPError()
        with raises(requests.HTTPError):
            self.pipeline._setup()

    def test_multiple_outputs(self, _):
        output_config = {"kafka_output": {}, "opensearch_output": {}}
        self.pipeline._logprep_config.update({"output": output_config})
        self.pipeline._setup()
        assert isinstance(self.pipeline._output, dict)
        assert len(self.pipeline._output) == 2

    def test_output_creates_real_outputs(self, _):
        self.pipeline._logprep_config["output"] = {
            "dummy1": {"type": "dummy_output", "default": False},
            "dummy2": {"type": "dummy_output"},
        }
        with mock.patch("logprep.factory.Factory.create", original_create):
            output = self.pipeline._output
            assert isinstance(output, dict)
            for output_connector in output.items():
                assert isinstance(output_connector[1], Output)
        assert not self.pipeline._output["dummy1"].default


class TestPipelineWithActualInput:
    def setup_method(self):
        self.config = GetterFactory.from_string("tests/testdata/config/config.yml").get_yaml()
        del self.config["output"]
        self.config["process_count"] = 1
        self.config["input"] = {
            "test_input": {
                "type": "dummy_input",
                "documents": [],
                "preprocessing": {"log_arrival_time_target_field": "arrival_time"},
            }
        }

    def test_pipeline_without_output_connector_and_one_input_event_and_preprocessors(self):
        self.config["input"]["test_input"]["documents"] = [{"applyrule": "yes"}]
        pipeline = Pipeline(config=self.config)
        assert pipeline._output is None
        event, extra_outputs = pipeline.process_pipeline()
        assert event["label"] == {"reporter": ["windows"]}
        assert "arrival_time" in event
        assert extra_outputs == []

    def test_pipeline_without_connectors_and_with_that_no_preprocessors(self):
        event = {"applyrule": "yes"}
        expected_event = {"applyrule": "yes", "label": {"reporter": ["windows"]}}
        del self.config["input"]
        pipeline = Pipeline(config=self.config)
        assert pipeline._output is None
        assert pipeline._input is None
        pipeline.process_event(event)
        assert event == expected_event

    def test_pipeline_without_output_connector_and_two_input_events_and_preprocessors(self):
        input_events = [
            {"applyrule": "yes"},
            {"winlog": {"event_data": {"IpAddress": "123.132.113.123"}}},
        ]
        self.config["input"]["test_input"]["documents"] = input_events
        pipeline = Pipeline(config=self.config)
        assert pipeline._output is None
        event, extra_outputs = pipeline.process_pipeline()
        assert event["label"] == {"reporter": ["windows"]}
        assert "arrival_time" in event
        assert extra_outputs == []
        event, extra_outputs = pipeline.process_pipeline()
        assert "pseudonym" in event.get("winlog", {}).get("event_data", {}).get("IpAddress")
        assert "arrival_time" in event
        assert len(extra_outputs) == 1

    def test_pipeline_hmac_error_message_without_output_connector(self):
        self.config["input"]["test_input"]["documents"] = [{"applyrule": "yes"}]
        self.config["input"]["test_input"]["preprocessing"] = {
            "hmac": {"target": "non_existing_field", "key": "secret", "output_field": "hmac"}
        }
        pipeline = Pipeline(config=self.config)
        assert pipeline._output is None
        event, _ = pipeline.process_pipeline()
        assert event["hmac"]["hmac"] == "error"

    def test_pipeline_run_raises_assertion_when_run_without_input(self):
        del self.config["input"]
        pipeline = Pipeline(config=self.config)
        with pytest.raises(
            AssertionError, match="Pipeline should not be run without input connector"
        ):
            pipeline.run()

    def test_pipeline_run_raises_assertion_when_run_without_output(self):
        pipeline = Pipeline(config=self.config)
        with pytest.raises(
            AssertionError, match="Pipeline should not be run without output connector"
        ):
            pipeline.run()

    def test_process_pipeline_raises_assertion_when_no_input_connector_is_set(self):
        del self.config["input"]
        pipeline = Pipeline(config=self.config)
        with pytest.raises(
            AssertionError, match="Run process_pipeline only with an valid input connector"
        ):
            pipeline.process_pipeline()


class TestMultiprocessingPipeline(ConfigurationForTests):
    def setup_class(self):
        self.log_handler = MultiprocessingLogHandler(DEBUG)

    def test_fails_if_log_handler_is_not_a_multiprocessing_log_handler(self):
        for not_a_log_handler in [None, 123, 45.67, TestMultiprocessingPipeline()]:
            with raises(MustProvideAnMPLogHandlerError):
                MultiprocessingPipeline(
                    pipeline_index=1,
                    config=self.logprep_config,
                    log_handler=not_a_log_handler,
                    lock=self.lock,
                    used_server_ports=mock.MagicMock(),
                    shared_dict=self.shared_dict,
                )

    def test_does_not_fail_if_log_handler_is_a_multiprocessing_log_handler(self):
        try:
            MultiprocessingPipeline(
                pipeline_index=1,
                config=self.logprep_config,
                log_handler=self.log_handler,
                lock=self.lock,
                used_server_ports=mock.MagicMock(),
                shared_dict=self.shared_dict,
            )
        except MustProvideAnMPLogHandlerError:
            fail("Must not raise this error for a correct handler!")

    def test_creates_a_new_process(self):
        children_before = active_children()
        children_running = self.start_and_stop_pipeline(
            MultiprocessingPipeline(
                pipeline_index=1,
                config=self.logprep_config,
                log_handler=self.log_handler,
                lock=self.lock,
                used_server_ports=mock.MagicMock(),
                shared_dict=self.shared_dict,
            )
        )

        assert len(children_running) == (len(children_before) + 1)

    def test_stop_terminates_the_process(self):
        children_running = self.start_and_stop_pipeline(
            MultiprocessingPipeline(
                pipeline_index=1,
                config=self.logprep_config,
                log_handler=self.log_handler,
                lock=self.lock,
                used_server_ports=mock.MagicMock(),
                shared_dict=self.shared_dict,
            )
        )
        children_after = active_children()

        assert len(children_after) == (len(children_running) - 1)

    def test_enable_iteration_sets_iterate_to_true_stop_to_false(self):
        pipeline = MultiprocessingPipeline(
            pipeline_index=1,
            config=self.logprep_config,
            log_handler=self.log_handler,
            lock=self.lock,
            used_server_ports=mock.MagicMock(),
            shared_dict=self.shared_dict,
        )
        assert not pipeline._iterate()

        pipeline._enable_iteration()
        assert pipeline._iterate()

        pipeline.stop()
        assert not pipeline._iterate()

    def test_graceful_shutdown_of_pipeline_on_source_disconnected_error(self, capfd):
        pipeline = MultiprocessingPipeline(
            pipeline_index=1,
            config=self.logprep_config,
            log_handler=self.log_handler,
            lock=self.lock,
            used_server_ports=mock.MagicMock(),
            shared_dict=self.shared_dict,
        )
        pipeline._input = mock.MagicMock()
        pipeline._input.get_next = mock.MagicMock()
        pipeline._input.get_next.side_effect = SourceDisconnectedError()
        pipeline.start()
        pipeline.stop()
        pipeline.join()
        out, err = capfd.readouterr()
        assert "AttributeError: 'bool' object has no attribute 'get_lock'" not in err

    def test_graceful_shutdown_of_pipeline_on_fata_input_error(self, capfd):
        pipeline = MultiprocessingPipeline(
            pipeline_index=1,
            config=self.logprep_config,
            log_handler=self.log_handler,
            lock=self.lock,
            used_server_ports=mock.MagicMock(),
            shared_dict=self.shared_dict,
        )
        pipeline._input = mock.MagicMock()
        pipeline._input.get_next = mock.MagicMock()
        pipeline._input.get_next.side_effect = FatalInputError()
        pipeline.start()
        pipeline.stop()
        pipeline.join()
        out, err = capfd.readouterr()
        assert "AttributeError: 'bool' object has no attribute 'get_lock'" not in err

    def test_graceful_shutdown_of_pipeline_on_fatal_output_error(self, capfd):
        pipeline = MultiprocessingPipeline(
            pipeline_index=1,
            config=self.logprep_config,
            log_handler=self.log_handler,
            lock=self.lock,
            used_server_ports=mock.MagicMock(),
            shared_dict=self.shared_dict,
        )
        pipeline._output = mock.MagicMock()
        pipeline._output.store = mock.MagicMock()
        pipeline._output.store.side_effect = FatalOutputError()
        pipeline.start()
        pipeline.stop()
        pipeline.join()
        out, err = capfd.readouterr()
        assert "AttributeError: 'bool' object has no attribute 'get_lock'" not in err

    @staticmethod
    def start_and_stop_pipeline(wrapper):
        wrapper.start()
        children_running = active_children()

        wrapper.stop()
        wrapper.join()

        return children_running
