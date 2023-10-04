import pytest

from rasa.dialogue_understanding.commands import SetSlotCommand, StartFlowCommand
from rasa.dialogue_understanding.processor.command_processor import (
    contains_command,
    _get_commands_from_tracker,
)
from rasa.shared.core.events import SlotSet, UserUttered
from rasa.shared.core.trackers import DialogueStateTracker


@pytest.mark.parametrize(
    "commands, command_type, expected_result",
    [
        ([SetSlotCommand("slot_name", "slot_value")], SetSlotCommand, True),
        ([StartFlowCommand("flow_name")], StartFlowCommand, True),
        (
            [StartFlowCommand("flow_name"), SetSlotCommand("slot_name", "slot_value")],
            StartFlowCommand,
            True,
        ),
        ([SetSlotCommand("slot_name", "slot_value")], StartFlowCommand, False),
    ],
)
def test_contains_command(commands, command_type, expected_result):
    """Test if commands contains a command of a given type."""
    # When
    result = contains_command(commands, command_type)
    # Then
    assert result == expected_result


def test_get_commands_from_tracker():
    """Test if commands are correctly extracted from tracker."""
    # Given
    tracker = DialogueStateTracker.from_events(
        "test",
        evts=[
            UserUttered("hi", {"name": "greet"}),
        ],
    )
    # use the conftest.py written by thomas in stack clean up pr.
    # When
    commands = _get_commands_from_tracker(tracker)
    # Then
    assert len(commands) == 2
    assert isinstance(commands[0], SetSlotCommand)
