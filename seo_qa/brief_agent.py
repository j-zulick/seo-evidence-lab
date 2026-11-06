"""Bounded Pydantic AI integration. Default demonstration uses TestModel only."""
from pydantic_ai import Agent,UsageLimits
from pydantic_ai.models.test import TestModel
from .models import RequirementInput

def fixture_requirement_draft():
    expected={'title':'Dummy brief extraction','urls':['https://fixture.example/category/coffee-tables'],'expected':{'indexable':True}}
    agent=Agent(TestModel(custom_output_args=expected),output_type=RequirementInput,
                instructions='Draft requirements from supplied evidence. Missing facts stay unresolved; output is not approval.')
    result=agent.run_sync('Fixture brief: verify the dummy category permits indexing.',usage_limits=UsageLimits(request_limit=1))
    return {'mode':'TestModel fixture, not live extraction quality','draft':result.output.model_dump(),'approved':False,'paid_calls':0}

if __name__=='__main__':
    import json
    print(json.dumps(fixture_requirement_draft(),indent=2))
