import glob
from pathlib import Path
import re
import textwrap
from typing import List, Optional, Tuple
import streamlit as st
from streamlit_ace import st_ace
from streamlit_chat import message
import openai
from ruamel.yaml import YAMLError

import networkx as nx
from rasa.shared.core.flows.yaml_flows_io import YAMLFlowsReader
from rasa.shared.utils.io import read_yaml

from dotenv import load_dotenv


st.set_page_config(layout="wide")
load_dotenv()  # load the openai key from the .env file

node_styles = {
    "question": {"fillcolor": "lightblue", "style": "filled", "shape": "rect"},
    "intent": {"fillcolor": "aquamarine", "style": "filled"},
    "action": {"fillcolor": "grey", "style": "filled"},
    "flow": {"fillcolor": "yellow", "style": "filled"},
    "link": {"fillcolor": "orange", "style": "filled"},
}


def openai_predict(skill, history):
    histroy_as_messages = [
        {
            "role": "user" if (is_user or is_user is None) else "assistant",
            "content": text,
        }
        for is_user, text in history
    ]

    histroy_as_messages.append({"role": "user", "content": skill})

    start_prompt = [
        {
            "role": "system",
            "content": "This is a conversation with a user creating a flow. You respond with the flow specification in YAML.",
        },
        {
            "role": "user",
            "content": textwrap.dedent(
                """\
            Please use the following YAML flow Syntax: 
            ```
            description: the description of the flow
            steps:
            - id: "0"   # id of the step
              intent: greet   # class of the user message
              next: "1"  # id of the next step
            - id: "1"   # id of the step
              action: utter_greet   # if a custom action needs to be triggered
              next: "2"  # id of the next step
            - id: "2"
              question: age   # if a question needs to be asked
              next:  # if there are multiple next steps, use conditions
              - if: age < 18
                then: "3"
              - if: age > 18 and age < 100
                then: "4"
              - else: "5"
            ```
            A step must always have exatly one of the following properties:
            - `intent`, if the step describes a user message
            - `action`, if the step describes an action the bot triggers, e.g. to send a message to the user
            - `question`, if the step describes a question the bot asks

            All steps should be part of the `steps` property of the flow. All
            steps should have a unique `id`. The first step should have the id
            `0`. The last step should not have a `next` property. The `next`
            property can either be a string (id of the next step) or a list of
            conditions. Each condition should have an `if` property (condition
            to be evaluated) and a `then` property (id of the next step if the
            condition is true).

            The last step of a flow should not have a `next` property. If `next`
            is using a condition, every branch of the condition must 
            point to a valid step id.

            Use short names for the `action` and `question` properties, e.g. 
            `age` instead of `What is your age?`.

            A step should not point to it's own id as the next step. It should
            point to another step's id.
            """
            ),
        },
    ]

    close_prompt = [
        {
            "role": "system",
            "content": "Start your response with the YAML flow specification. Add any explanations, questions, or comments you have afterwards. Keep your explanations and comments short.",
        }
    ]

    # create a completion
    completion = openai.ChatCompletion.create(
        # model="gpt-3.5-turbo",
        model="gpt-4",
        messages=start_prompt + histroy_as_messages + close_prompt,
    )

    return completion.choices[0].message.content


def build_graph(flows) -> List[str]:
    graphs = []
    for flow_id, flow in flows.items():
        G = nx.MultiDiGraph(splines="false")
        steps = flow["steps"]

        def add_node(step, type):
            G.add_node(
                step["id"],
                label=f"{type}: {step[type]}",
                fillcolor=node_styles[type]["fillcolor"],
                style=node_styles[type]["style"],
                shape=node_styles[type].get("shape", "oval"),
            )

        for step in steps:
            if "question" in step:
                add_node(step, "question")
            elif "intent" in step:
                add_node(step, "intent")
            elif "action" in step:
                add_node(step, "action")
            elif "flow" in step:
                add_node(step, "flow")
            elif "link" in step:
                add_node(step, "link")

        for step in steps:
            if "next" in step:
                if isinstance(step["next"], str):
                    G.add_edge(step["id"], step["next"])
                else:
                    for condition in step["next"]:
                        if "then" in condition:
                            G.add_edge(
                                step["id"],
                                condition["then"],
                                label=condition["if"],
                            )
                        elif "else" in condition:
                            G.add_edge(
                                step["id"],
                                condition["else"],
                                label="else",
                            )
        graphs.append(nx.nx_pydot.to_pydot(G).to_string())
    return graphs


def parse_yaml_from_response(response) -> Optional[str]:
    """Parse yaml from a markdown block.

    Uses a regular expression to match the ``` code block."""
    pattern = re.compile(r"```(yaml)?(.*?)```", re.DOTALL)
    match = pattern.search(response)

    return match.group(2) if match else None


def remove_yaml_from_response(response) -> str:
    """Remove yaml from a markdown block.

    Uses a regular expression to match the ``` code block."""
    pattern = re.compile(r"```(yaml)?(.*?)```", re.DOTALL)
    return pattern.sub("", response)


def display_chat():
    if not st.session_state["past"]:
        return

    for idx, (is_user, text) in enumerate(reversed(st.session_state["past"])):
        if is_user is not None:
            message(text, is_user=is_user, key=idx)


# installation:
# pip install streamlit
# pip install streamlit-ace
# pip install streamlit-chat
# pip install openai
# pip install python-dotenv

# list files in the data directory of the current folder


def get_text():
    input_text = st.text_input(
        "You: ",
        value="",
        key="input",
    )
    return input_text


def i_updated_yaml_manually(updated_yaml):
    return textwrap.dedent(
        f"""\
        I have updated the YAML manually. Here is the new YAML:
        ```
        {updated_yaml}
        ```"""
    )


def ask_llm_for_yaml(text_input: str, state: List[Tuple[(bool, str)]]):
    response = openai_predict(text_input, state)
    yaml_code = parse_yaml_from_response(response)

    if yaml_code:
        try:
            r = read_yaml(yaml_code)
            return response
        except YAMLError as error:
            # reprompt the llm to fix the yaml
            retry_state = state.copy() + [(True, text_input)] + [(False, response)]
            return ask_llm_for_yaml(
                f"The YAML is not valid. Here is the error I got: {error}",
                retry_state,
            )
    else:
        # TODO: request failed. got to reprompt llm
        return response


if __name__ == "__main__":
    base_dir = "/Users/tmbo/lastmile/bot-ai/rasa-project-with-flows"
    path = f"{base_dir}/data/*.yml"
    files = glob.glob(path)

    selected_file = st.selectbox(
        "Yaml file",
        [Path(f).relative_to(base_dir) for f in files],
    )

    left, middle, right = st.columns([2, 2, 3])
    if "past" not in st.session_state:
        st.session_state["past"] = []

    if "current_yaml" not in st.session_state:
        st.session_state["current_yaml"] = ""

    with left:
        with st.form("my_form", clear_on_submit=True):
            user_input = get_text()
            submitted = st.form_submit_button("➡")
            if submitted and user_input:
                # add streamlit spinne
                with st.spinner("Thinking..."):
                    response = ask_llm_for_yaml(user_input, st.session_state.past)
                    st.session_state.past.append((True, user_input))
                    st.session_state.past.append(
                        (False, remove_yaml_from_response(response))
                    )

                    yaml_code = parse_yaml_from_response(response)
                    if yaml_code:
                        st.session_state.current_yaml = yaml_code

        display_chat()

    with middle:
        # Spawn a new Ace editor
        if st.session_state.current_yaml:
            updated = st_ace(value=st.session_state.current_yaml, language="yaml")

            if updated and updated != st.session_state.current_yaml:
                st.session_state.current_yaml = updated
                st.session_state.past.append((None, i_updated_yaml_manually(updated)))

            # TODO: write out to file
            # with open(Path(base_dir) / selected_file, "w") as f:
            #     f.write(content)

    with right:
        if st.session_state.current_yaml:
            r = read_yaml(st.session_state.current_yaml)

            graphs = build_graph({"main_flow": r})
            try:
                YAMLFlowsReader.read_from_string(st.session_state.current_yaml)
                for graph in graphs:
                    # st.write(graph)
                    st.graphviz_chart(graph)
            except Exception as e:
                st.error(str(e))
