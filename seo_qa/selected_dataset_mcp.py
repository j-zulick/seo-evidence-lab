"""Read-only stdio tools for a startup-selected dataset and expected content hash."""
import os
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from .selected_dataset import SelectedDataset


def create_server(dataset):
    server = FastMCP('SEO QA selected local dataset')
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    @server.tool(annotations=readonly)
    def get_dataset_manifest() -> dict:
        """List selected audit IDs and content identity without filesystem paths."""
        return {'dataset_sha256': dataset.sha256, 'label': dataset.payload['label'],
                'audit_ids': list(dataset._audits), 'passage_count': len(dataset.payload['passages'])}

    @server.tool(annotations=readonly)
    def get_selected_finding(audit_id: str) -> dict:
        """Read an exact replay-verified audit from the selected dataset."""
        return dataset.evidence(audit_id)

    @server.tool(annotations=readonly)
    def compare_selected_audits(before_id: str, after_id: str) -> dict:
        """Compare two explicitly selected audit identities with immutable pins."""
        return dataset.comparison(before_id, after_id)

    @server.tool(annotations=readonly)
    def search_selected_passages(query: str) -> dict:
        """Search selected passages with fixed lexical expansion and source references."""
        return dataset.search(query)

    return server


if __name__ == '__main__':
    dataset = SelectedDataset.from_file(os.environ['SEO_QA_DATASET'], os.environ['SEO_QA_DATASET_SHA256'])
    create_server(dataset).run(transport='stdio')
