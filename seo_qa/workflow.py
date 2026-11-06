"""LangGraph coordinates deterministic stages; Store owns durable job state."""
from typing import TypedDict
from langgraph.graph import StateGraph, START, END


class State(TypedDict,total=False):
    requirement_id: str
    run_id: str
    report: dict


def build_workflow(store,inspector):
    def admission(state):
        run_id=state.get('run_id')
        return {'run_id':run_id or store.create_run(state['requirement_id'])['id']}
    def capture(state):
        store.execute_run(state['run_id'],inspector)
        return {}
    def report(state):
        return {'report':store.export_report(state['run_id'])}
    graph=StateGraph(State)
    graph.add_node('admit',admission)
    graph.add_node('inspect',capture)
    graph.add_node('report',report)
    graph.add_edge(START,'admit')
    graph.add_edge('admit','inspect')
    graph.add_edge('inspect','report')
    graph.add_edge('report',END)
    return graph.compile()
