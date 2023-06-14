from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Text, List, Optional, Union
import re
import logging
import importlib.resources
from jinja2 import Template

from rasa.core.constants import (
    DEFAULT_POLICY_PRIORITY,
    POLICY_MAX_HISTORY,
    POLICY_PRIORITY,
)
from pypred import Predicate
from rasa.core.policies.rule_policy import RulePolicy
from rasa.shared.constants import FLOW_PREFIX
from rasa.shared.nlu.constants import ENTITY_ATTRIBUTE_TYPE, INTENT_NAME_KEY
from rasa.shared.core.constants import (
    ACTION_FLOW_CONTINUE_INERRUPTED_NAME,
    ACTION_LISTEN_NAME,
    FLOW_STACK_SLOT,
)
from rasa.shared.core.events import ActiveLoop, Event, SlotSet
from rasa.shared.core.flows.flow import (
    END_STEP,
    START_STEP,
    ActionFlowStep,
    ElseFlowLink,
    EndFlowStep,
    Flow,
    FlowStep,
    FlowsList,
    IfFlowLink,
    QuestionScope,
    UserMessageStep,
    LinkFlowStep,
    SetSlotsFlowStep,
    QuestionFlowStep,
    StaticFlowLink,
)
from rasa.core.featurizers.tracker_featurizers import TrackerFeaturizer
from rasa.core.policies.policy import Policy, PolicyPrediction, SupportedData
from rasa.engine.graph import ExecutionContext
from rasa.engine.recipes.default_recipe import DefaultV1Recipe
from rasa.engine.storage.resource import Resource
from rasa.engine.storage.storage import ModelStorage
from rasa.shared.core.domain import Domain
from rasa.shared.core.generator import TrackerWithCachedStates
from rasa.shared.core.slots import Slot
from rasa.shared.core.trackers import (
    DialogueStateTracker,
)
from rasa.utils.llm import tracker_as_readable_transcript, generate_text_openai_chat
from rasa.core.policies.detectors import SensitiveTopicDetector

logger = logging.getLogger(__name__)

SENSITIVE_TOPIC_DETECTOR_CONFIG_KEY = "sensitive_topic_detector"

PROMPT_TEMPLATE = Template(
    importlib.resources.read_text("rasa.core.policies", "flow_prompt_template.jinja2")
)


def parse_action_list(actions: str, flows: FlowsList) -> ActionPrediction:
    start_flow_actions = []
    slot_sets = []
    slot_set_re = re.compile(r"SetSlot\(([a-zA-Z_][a-zA-Z0-9_-]*?),([^)]*)\)")
    start_flow_re = re.compile(r"StartFlow\(([a-zA-Z_][a-zA-Z0-9_-]*?)\)")
    for action in actions.strip().splitlines():
        if m := slot_set_re.search(action):
            slot_sets.append(SlotSet(m.group(1).strip(), m.group(2).strip()))
        elif m := start_flow_re.search(action):
            start_flow_actions.append(m.group(1).strip())

    if len(start_flow_actions) > 0:
        return ActionPrediction(
            FLOW_PREFIX + start_flow_actions[0], 1.0, events=slot_sets
        )
    else:
        return ActionPrediction(None, 1.0, events=slot_sets)


def create_flow_inputs(flows: FlowsList) -> List[dict[str, str]]:
    result = []
    for flow in flows.underlying_flows:
        result.append(
            {
                "name": flow.id,
                "description": flow.description,
                "slots": ", ".join(flow.slots()),
            }
        )
    return result


def render_template(tracker: DialogueStateTracker, flows: FlowsList) -> str:
    flow_stack = FlowStack.from_tracker(tracker)
    top_flow = flow_stack.top_flow(flows) if flow_stack is not None else None
    current_step = flow_stack.top_flow_step(flows) if flow_stack is not None else None
    if top_flow is not None:
        flow_slots = [
            {
                "name": k,
                "value": (tracker.get_slot(k) or "undefined"),
                "type": tracker.slots[k].type_name,
            }
            for k in top_flow.slots()
        ]
    else:
        flow_slots = []

    question = (
        current_step.question
        if current_step is not None and isinstance(current_step, QuestionFlowStep)
        else None
    )

    inputs = {
        "available_flows": create_flow_inputs(flows),
        "current_conversation": tracker_as_readable_transcript(tracker),
        "flow_slots": flow_slots,
        "current_flow": top_flow.id if top_flow is not None else None,
        "question": question,
    }

    return PROMPT_TEMPLATE.render(**inputs)


class FlowException(Exception):
    """Exception that is raised when there is a problem with a flow."""

    pass


@DefaultV1Recipe.register(
    DefaultV1Recipe.ComponentType.POLICY_WITHOUT_END_TO_END_SUPPORT, is_trainable=False
)
class FlowPolicy(Policy):
    """A policy which handles the flow of the conversation based on flows.

    Flows are loaded from files during training. During prediction,
    the flows are applied.
    """

    @staticmethod
    def get_default_config() -> Dict[Text, Any]:
        """Returns the default config (see parent class for full docstring)."""
        # please make sure to update the docs when changing a default parameter
        return {
            POLICY_PRIORITY: DEFAULT_POLICY_PRIORITY,
            POLICY_MAX_HISTORY: None,
            SENSITIVE_TOPIC_DETECTOR_CONFIG_KEY: None,
        }

    @staticmethod
    def supported_data() -> SupportedData:
        """The type of data supported by this policy.

        By default, this is only ML-based training data. If policies support rule data,
        or both ML-based data and rule data, they need to override this method.

        Returns:
            The data type supported by this policy (ML-based training data).
        """
        return SupportedData.ML_DATA

    def __init__(
        self,
        config: Dict[Text, Any],
        model_storage: ModelStorage,
        resource: Resource,
        execution_context: ExecutionContext,
        featurizer: Optional[TrackerFeaturizer] = None,
    ) -> None:
        """Constructs a new Policy object."""
        super().__init__(config, model_storage, resource, execution_context, featurizer)

        self.max_history = self.config.get(POLICY_MAX_HISTORY)
        self.resource = resource

        if detector_config := self.config.get(SENSITIVE_TOPIC_DETECTOR_CONFIG_KEY):
            # if the detector is configured, we need to load it
            full_config = SensitiveTopicDetector.get_default_config()
            full_config.update(detector_config)
            self._sensitive_topic_detector = SensitiveTopicDetector(full_config)
        else:
            self._sensitive_topic_detector = None

    def train(
        self,
        training_trackers: List[TrackerWithCachedStates],
        domain: Domain,
        **kwargs: Any,
    ) -> Resource:
        """Trains a policy.

        Args:
            training_trackers: The story and rules trackers from the training data.
            domain: The model's domain.
            **kwargs: Depending on the specified `needs` section and the resulting
                graph structure the policy can use different input to train itself.

        Returns:
            A policy must return its resource locator so that potential children nodes
            can load the policy from the resource.
        """
        # currently, nothing to do here. we have access to the flows during
        # prediction. we might want to store the flows in the future
        # or do some preprocessing here.
        return self.resource

    def _llm_state_update(
        self, tracker: DialogueStateTracker, flows: Optional[FlowsList]
    ) -> ActionPrediction:
        if self._is_first_prediction_after_user_message(tracker):
            logger.info(flows)
            flow_prompt = render_template(tracker, flows or FlowsList([]))
            logger.info(flow_prompt)
            action_list = generate_text_openai_chat(flow_prompt)
            logger.info(action_list)
            prediction = parse_action_list(action_list, flows or FlowsList([]))
            logger.info(prediction)
            return prediction
        else:
            return ActionPrediction(None, 0.0)

    @staticmethod
    def _is_first_prediction_after_user_message(tracker: DialogueStateTracker) -> bool:
        """Checks whether the tracker ends with an action listen.

        If the tracker ends with an action listen, it means that we've just received
        a user message.

        Args:
            tracker: The tracker.

        Returns:
            `True` if the tracker is the first one after a user message, `False`
            otherwise.
        """
        return tracker.latest_action_name == ACTION_LISTEN_NAME

    def predict_action_probabilities(
        self,
        tracker: DialogueStateTracker,
        domain: Domain,
        rule_only_data: Optional[Dict[Text, Any]] = None,
        flows: Optional[FlowsList] = None,
        **kwargs: Any,
    ) -> PolicyPrediction:
        """Predicts the next action the bot should take after seeing the tracker.

        Args:
            tracker: The tracker containing the conversation history up to now.
            domain: The model's domain.
            rule_only_data: Slots and loops which are specific to rules and hence
                should be ignored by this policy.
            **kwargs: Depending on the specified `needs` section and the resulting
                graph structure the policy can use different input to make predictions.

        Returns:
             The prediction.
        """
        predicted_action = None
        if (
            self._sensitive_topic_detector
            and self._is_first_prediction_after_user_message(tracker)
            and (latest_message := tracker.latest_message)
        ):
            if self._sensitive_topic_detector.check(latest_message.text):
                predicted_action = self._sensitive_topic_detector.action()
                # TODO: in addition to predicting an action, we need to make
                #   sure that the input isn't used in any following flow
                #   steps. At the same time, we can't completely skip flows
                #   as we want to guide the user to the next step of the flow.
                logger.info(
                    "Sensitive topic detected, predicting action %s", predicted_action
                )
            else:
                logger.info("No sensitive topic detected: %s", latest_message.text)

        # if detector predicted an action, we don't want to predict a flow
        if predicted_action is not None:
            return self._create_prediction_result(predicted_action, domain, 1.0, [])

        executor = FlowExecutor.from_tracker(tracker, flows or FlowsList([]))

        collected_events = []

        prediction = self._llm_state_update(tracker, flows)
        if prediction.action_name is not None:
            return self._create_prediction_result(
                prediction.action_name,
                domain,
                prediction.score,
                prediction.events,
                prediction.metadata,
            )
        else:
            collected_events.extend(prediction.events or [])

        # create executor and predict next action
        prediction = executor.advance_flows(tracker, domain)
        collected_events.extend(prediction.events or [])
        return self._create_prediction_result(
            prediction.action_name,
            domain,
            prediction.score,
            collected_events,
            prediction.metadata,
        )

    def _create_prediction_result(
        self,
        action_name: Optional[Text],
        domain: Domain,
        score: float = 1.0,
        events: Optional[List[Event]] = None,
        action_metadata: Optional[Dict[Text, Any]] = None,
    ) -> PolicyPrediction:
        """Creates a prediction result.

        Args:
            action_name: The name of the predicted action.
            domain: The model's domain.
            score: The score of the predicted action.

        Resturns:
            The prediction result where the score is used for one hot encoding.
        """
        result = self._default_predictions(domain)
        if action_name:
            result[domain.index_for_action(action_name)] = score
        return self._prediction(result, events=events, action_metadata=action_metadata)


@dataclass
class FlowStack:
    """Represents the current flow stack."""

    frames: List[FlowStackFrame]

    @staticmethod
    def from_dict(data: List[Dict[Text, Any]]) -> FlowStack:
        """Creates a `FlowStack` from a dictionary.

        Args:
            data: The dictionary to create the `FlowStack` from.

        Returns:
            The created `FlowStack`.
        """
        return FlowStack([FlowStackFrame.from_dict(frame) for frame in data])

    def as_dict(self) -> List[Dict[Text, Any]]:
        """Returns the `FlowStack` as a dictionary.

        Returns:
            The `FlowStack` as a dictionary.
        """
        return [frame.as_dict() for frame in self.frames]

    def push(self, frame: FlowStackFrame) -> None:
        """Pushes a new frame onto the stack.

        Args:
            frame: The frame to push onto the stack.
        """
        self.frames.append(frame)

    def update(self, frame: FlowStackFrame) -> None:
        """Updates the topmost frame.

        Args:
            frame: The frame to update.
        """
        if not self.is_empty():
            self.pop()

        self.push(frame)

    def advance_top_flow(self, updated_id: Text) -> None:
        """Updates the topmost flow step.

        Args:
            updated_id: The updated flow step ID.
        """
        if top := self.top():
            top.step_id = updated_id

    def pop(self) -> FlowStackFrame:
        """Pops the topmost frame from the stack.

        Returns:
            The popped frame.
        """
        return self.frames.pop()

    def top(self) -> Optional[FlowStackFrame]:
        """Returns the topmost frame from the stack.

        Returns:
            The topmost frame.
        """
        if self.is_empty():
            return None

        return self.frames[-1]

    def top_flow(self, flows: FlowsList) -> Optional[Flow]:
        """Returns the topmost flow from the stack.

        Args:
            flows: The flows to use.

        Returns:
            The topmost flow.
        """
        if not (top := self.top()):
            return None

        return flows.flow_by_id(top.flow_id)

    def top_flow_step(self, flows: FlowsList) -> Optional[FlowStep]:
        """Get the current flow step.

        Returns:
            The current flow step or `None` if no flow is active."""
        if not (top := self.top()) or not (top_flow := self.top_flow(flows)):
            return None

        return top_flow.step_for_id(top.step_id)

    def is_empty(self) -> bool:
        """Checks if the stack is empty.

        Returns:
            `True` if the stack is empty, `False` otherwise.
        """
        return len(self.frames) == 0

    @staticmethod
    def from_tracker(tracker: DialogueStateTracker) -> FlowStack:
        """Creates a `FlowStack` from a tracker.

        Args:
            tracker: The tracker to create the `FlowStack` from.

        Returns:
            The created `FlowStack`.
        """
        flow_stack = tracker.get_slot(FLOW_STACK_SLOT) or []
        return FlowStack.from_dict(flow_stack)


@dataclass
class ActionPrediction:
    """Represents an action prediction."""

    action_name: Optional[Text]
    """The name of the predicted action."""
    score: float
    """The score of the predicted action."""
    metadata: Optional[Dict[Text, Any]] = None
    """The metadata of the predicted action."""
    events: Optional[List[Event]] = None
    """The events attached to the predicted action."""


class StackFrameType(str, Enum):
    INTERRUPT = "interrupt"
    """The frame is an interrupt frame.

    This means that the previous flow was interrupted by this flow."""
    LINK = "link"
    """The frame is a link frame.

    This means that the previous flow linked to this flow."""
    REGULAR = "regular"
    """The frame is a regular frame.

    In all other cases, this is the case."""

    @staticmethod
    def from_str(typ: Optional[Text]) -> "StackFrameType":
        """Creates a `StackFrameType` from a string."""
        if typ is None:
            return StackFrameType.REGULAR
        elif typ == StackFrameType.INTERRUPT.value:
            return StackFrameType.INTERRUPT
        elif typ == StackFrameType.LINK.value:
            return StackFrameType.LINK
        elif typ == StackFrameType.REGULAR.value:
            return StackFrameType.REGULAR
        else:
            raise NotImplementedError


@dataclass
class FlowStackFrame:
    """Represents the current flow step."""

    flow_id: Text
    """The ID of the current flow."""
    step_id: Text = START_STEP
    """The ID of the current step."""
    frame_type: StackFrameType = StackFrameType.REGULAR
    """The type of the frame. Defaults to `StackFrameType.REGULAR`."""

    @staticmethod
    def from_dict(data: Dict[Text, Any]) -> FlowStackFrame:
        """Creates a `FlowStackFrame` from a dictionary.

        Args:
            data: The dictionary to create the `FlowStackFrame` from.

        Returns:
            The created `FlowStackFrame`.
        """
        return FlowStackFrame(
            data["flow_id"],
            data["step_id"],
            StackFrameType.from_str(data.get("frame_type")),
        )

    def as_dict(self) -> Dict[Text, Any]:
        """Returns the `FlowStackFrame` as a dictionary.

        Returns:
            The `FlowStackFrame` as a dictionary.
        """
        return {
            "flow_id": self.flow_id,
            "step_id": self.step_id,
            "frame_type": self.frame_type.value,
        }

    def with_updated_id(self, step_id: Text) -> FlowStackFrame:
        """Creates a copy of the `FlowStackFrame` with the given step id.

        Args:
            step_id: The step id to use for the copy.

        Returns:
            The copy of the `FlowStackFrame` with the given step id.
        """
        return FlowStackFrame(self.flow_id, step_id, self.frame_type)

    def __repr__(self) -> Text:
        return (
            f"FlowState(flow_id: {self.flow_id}, "
            f"step_id: {self.step_id}, "
            f"frame_type: {self.frame_type.value})"
        )


class FlowExecutor:
    """Executes a flow."""

    def __init__(self, flow_stack: FlowStack, all_flows: FlowsList) -> None:
        """Initializes the `FlowExecutor`.

        Args:
            flow_stack_frame: State of the flow.
            all_flows: All flows.
        """
        self.flow_stack = flow_stack
        self.all_flows = all_flows

    @staticmethod
    def from_tracker(tracker: DialogueStateTracker, flows: FlowsList) -> FlowExecutor:
        """Creates a `FlowExecutor` from a tracker.

        Args:
            tracker: The tracker to create the `FlowExecutor` from.
            flows: The flows to use.

        Returns:
            The created `FlowExecutor`."""
        flow_stack = FlowStack.from_tracker(tracker)
        return FlowExecutor(flow_stack, flows or FlowsList([]))

    def find_startable_flow(self, tracker: DialogueStateTracker) -> Optional[Flow]:
        """Finds a flow which can be started.

        Args:
            tracker: The tracker containing the conversation history up to now.
            domain: The model's domain.
            flows: The flows to use.

        Returns:
            The predicted action and the events to run.
        """
        if (
            not tracker.latest_message
            or tracker.latest_action_name != ACTION_LISTEN_NAME
        ):
            # flows can only be started automatically as a response to a user message
            return None
        latest_intent: Text = tracker.latest_message.intent.get(INTENT_NAME_KEY, "")
        latest_entities: List[Text] = [
            e.get(ENTITY_ATTRIBUTE_TYPE, "") for e in tracker.latest_message.entities
        ]

        for flow in self.all_flows.underlying_flows:
            first_step = flow.first_step_in_flow()
            if not first_step or not isinstance(first_step, UserMessageStep):
                continue

            if first_step.is_triggered(latest_intent, latest_entities):
                return flow
        return None

    @staticmethod
    def is_condition_satisfied(
        predicate: Text, domain: Domain, tracker: "DialogueStateTracker"
    ) -> bool:
        """Evaluate a predicate condition."""

        def get_value(
            initial_value: Union[Text, None]
        ) -> Union[Text, float, bool, None]:
            if initial_value is None or isinstance(initial_value, (bool, float)):
                return initial_value

            # if this isn't a bool or float, it's something else
            # the below is a best effort to convert it to something we can
            # use for the predicate evaluation
            initial_value = str(initial_value)  # make sure it's a string

            if initial_value.lower() in ["true", "false"]:
                return initial_value.lower() == "true"

            if initial_value.isnumeric():
                return float(initial_value)

            return initial_value

        text_slots = dict(
            {slot.name: get_value(tracker.get_slot(slot.name)) for slot in domain.slots}
        )
        p = Predicate(predicate)
        evaluation, _ = p.analyze(text_slots)
        return evaluation

    def _select_next_step_id(
        self, current: FlowStep, domain: Domain, tracker: "DialogueStateTracker"
    ) -> Optional[Text]:
        """Selects the next step id based on the current step."""
        next = current.next
        if len(next.links) == 1 and isinstance(next.links[0], StaticFlowLink):
            return next.links[0].target

        # evaluate if conditions
        for link in next.links:
            if isinstance(link, IfFlowLink) and link.condition:
                if self.is_condition_satisfied(link.condition, domain, tracker):
                    return link.target

        # evaluate else condition
        for link in next.links:
            if isinstance(link, ElseFlowLink):
                return link.target

        if next.links:
            raise ValueError(
                "No link was selected, but links are present. Links "
                "must cover all possible cases."
            )
        if current.id != END_STEP:
            # we've reached the end of the user defined steps in the flow.
            # every flow should end with an end step, so we add it here.
            return END_STEP
        else:
            # we are already at the very end of the flow. There is no next step.
            return None

    def _select_next_step(
        self,
        tracker: "DialogueStateTracker",
        domain: Domain,
        current_step: FlowStep,
        flow_id: Text,
    ) -> Optional[FlowStep]:
        """Get the next step to execute."""
        next_id = self._select_next_step_id(current_step, domain, tracker)
        if next_id is None:
            return None

        return self.all_flows.step_by_id(next_id, flow_id)

    def _slot_for_question(self, question: Text, domain: Domain) -> Slot:
        """Find the slot for a question."""
        for slot in domain.slots:
            if slot.name == question:
                return slot
        else:
            raise FlowException(
                f"Question '{question}' does not map to an existing slot."
            )

    def _is_step_completed(
        self, step: FlowStep, tracker: "DialogueStateTracker"
    ) -> bool:
        """Check if a step is completed."""
        if isinstance(step, QuestionFlowStep):
            return tracker.get_slot(step.question) is not None
        else:
            return True

    def consider_flow_switch(self, tracker: DialogueStateTracker) -> ActionPrediction:
        """Consider switching to a new flow.

        Args:
            tracker: The tracker to get the next action for.

        Returns:
            The predicted action and the events to run."""
        if new_flow := self.find_startable_flow(tracker):
            # there are flows available, but we are not in a flow
            # it looks like we can start a flow, so we'll predict the trigger action
            logger.debug(f"Found startable flow: {new_flow.id}")
            return ActionPrediction(FLOW_PREFIX + new_flow.id, 1.0)
        else:
            logger.debug("No startable flow found.")
            return ActionPrediction(None, 0.0)

    def advance_flows(
        self, tracker: DialogueStateTracker, domain: Domain
    ) -> ActionPrediction:
        """Advance the flows.

        Either start a new flow or advance the current flow.

        Args:
            tracker: The tracker to get the next action for.
            domain: The domain to get the next action for.

        Returns:
            The predicted action and the events to run."""

        prediction = self.consider_flow_switch(tracker)

        if prediction.action_name:
            # if a flow can be started, we'll start it
            return prediction
        if self.flow_stack.is_empty():
            # if there are no flows, there is nothing to do
            return ActionPrediction(None, 0.0)
        else:
            prediction = self._select_next_action(tracker, domain)
            if FlowStack.from_tracker(tracker).as_dict() != self.flow_stack.as_dict():
                # we need to update the flow stack to persist the state of the executor
                if not prediction.events:
                    prediction.events = []
                prediction.events.append(
                    SlotSet(
                        FLOW_STACK_SLOT,
                        self.flow_stack.as_dict(),
                    )
                )
            return prediction

    def _select_next_action(
        self,
        tracker: DialogueStateTracker,
        domain: Domain,
    ) -> ActionPrediction:
        """Select the next action to execute.

        Advances the current flow and returns the next action to execute. A flow
        is advanced until it is completed or until it predicts an action. If
        the flow is completed, the next flow is popped from the stack and
        advanced. If there are no more flows, the action listen is predicted.

        Args:
            tracker: The tracker to get the next action for.
            domain: The domain to get the next action for.

        Returns:
            The next action to execute, the events that should be applied to the
            tracker and the confidence of the prediction."""

        predicted_action: Optional[ActionPrediction] = None
        gathered_events = []

        while not predicted_action or predicted_action.score == 0.0:
            if not (current_flow := self.flow_stack.top_flow(self.all_flows)):
                # If there is no current flow, we assume that all flows are done
                # and there is nothing to do. The assumption here is that every
                # flow ends with an action listen.
                predicted_action = ActionPrediction(ACTION_LISTEN_NAME, 1.0)
                break

            if not (previous_step := self.flow_stack.top_flow_step(self.all_flows)):
                raise FlowException(
                    "The current flow is set, but there is no current step. "
                    "This should not happen, if a flow is started it should be set "
                    "to __start__ if it ended it should be popped from the stack."
                )

            logger.info(previous_step)
            predicted_action = self._wrap_up_previous_step(
                current_flow, previous_step, tracker
            )
            gathered_events.extend(predicted_action.events or [])

            if predicted_action.action_name:
                # if the previous step predicted an action, we'll stop here
                # the step is not completed yet and we need to predict the
                # action first before we can try again to wrap up this step and
                # advance to the next one
                break

            current_step = self._select_next_step(
                tracker, domain, previous_step, current_flow.id
            )
            if current_step is None:
                frame = self.flow_stack.pop()
                if frame.frame_type == StackFrameType.INTERRUPT:
                    # if the previous frame got interrupted, we need to run the step
                    # that got interrupted again
                    current_step = self.flow_stack.top_flow_step(self.all_flows)

            if current_step:
                # this can't be an else, because the previous if might change
                # this to "not None"
                self.flow_stack.advance_top_flow(current_step.id)

                predicted_action = self._run_step(current_flow, current_step, tracker)
                gathered_events.extend(predicted_action.events or [])

        predicted_action.events = gathered_events
        return predicted_action

    def _reset_scoped_slots(
        self, current_flow: Flow, tracker: DialogueStateTracker
    ) -> List[Event]:
        """Reset all scoped slots."""
        events: List[Event] = []
        for step in current_flow.steps:
            # reset all slots scoped to the flow
            if isinstance(step, QuestionFlowStep) and step.scope == QuestionScope.FLOW:
                slot = tracker.slots.get(step.question, None)
                initial_value = slot.initial_value if slot else None
                events.append(SlotSet(step.question, initial_value))
        return events

    def _wrap_up_previous_step(
        self,
        flow: Flow,
        step: FlowStep,
        tracker: DialogueStateTracker,
    ) -> ActionPrediction:
        """Try to wrap up the previous step.

        Args:
            current_flow: The current flow.
            step: The previous step.
            tracker: The tracker to run the step on.

        Returns:
            The predicted action and the events to run."""
        if isinstance(step, QuestionFlowStep):
            # the question is only finished once the slot is set and the loop
            # is finished
            active_loop_name, _ = RulePolicy._find_action_from_loop_happy_path(tracker)
            if active_loop_name:
                # loop is not yet done
                return ActionPrediction(active_loop_name, 1.0)
            else:
                return ActionPrediction(None, 0.0)
        else:
            return ActionPrediction(None, 0.0)

    def _run_step(
        self,
        flow: Flow,
        step: FlowStep,
        tracker: DialogueStateTracker,
    ) -> ActionPrediction:
        """Run a single step of a flow.

        Returns the predicted action and a list of events that were generated
        during the step. The predicted action can be `None` if the step
        doesn't generate an action. The list of events can be empty if the
        step doesn't generate any events.

        Raises a `FlowException` if the step is invalid.

        Args:
            flow: The flow that the step belongs to.
            step: The step to run.
            tracker: The tracker to run the step on.

        Returns:
            A tuple of the predicted action and a list of events."""
        if isinstance(step, QuestionFlowStep):
            slot = tracker.slots.get(step.question, None)
            initial_value = slot.initial_value if slot else None
            if step.skip_if_filled and slot.value != initial_value:
                return ActionPrediction(None, 0.0)

            question_action = ActionPrediction("question_" + step.question, 1.0)
            if slot.value != initial_value:
                question_action.events = [SlotSet(step.question, initial_value)]
            return question_action

        elif isinstance(step, ActionFlowStep):
            if not step.action:
                raise FlowException(f"Action not specified for step {step}")
            return ActionPrediction(step.action, 1.0)
        elif isinstance(step, LinkFlowStep):
            self.flow_stack.push(
                FlowStackFrame(
                    flow_id=step.link,
                    step_id=START_STEP,
                    frame_type=StackFrameType.LINK,
                )
            )
            if tracker.active_loop_name:
                return ActionPrediction(None, 0.0, events=[ActiveLoop(None)])
            else:
                return ActionPrediction(None, 0.0)
        elif isinstance(step, SetSlotsFlowStep):
            return ActionPrediction(
                None,
                0.0,
                events=[SlotSet(slot["key"], slot["value"]) for slot in step.slots],
            )
        elif isinstance(step, UserMessageStep):
            return ActionPrediction(None, 0.0)
        elif isinstance(step, EndFlowStep):
            # this is the end of the flow, so we'll pop it from the stack
            events = self._reset_scoped_slots(flow, tracker)
            if len(self.flow_stack.frames) >= 2:
                previous_frame = self.flow_stack.frames[-2]
                current_frame = self.flow_stack.frames[-1]

                if current_frame.frame_type == StackFrameType.INTERRUPT:
                    # get stack frame that is below the current one and which will
                    # be continued now that this one has ended.
                    previous_flow = self.all_flows.flow_by_id(previous_frame.flow_id)
                    previous_flow_name = previous_flow.name if previous_flow else None
                    return ActionPrediction(
                        ACTION_FLOW_CONTINUE_INERRUPTED_NAME,
                        1.0,
                        metadata={"flow_name": previous_flow_name},
                        events=events,
                    )
            return ActionPrediction(None, 0.0, events=events)
        else:
            raise FlowException(f"Unknown flow step type {type(step)}")
