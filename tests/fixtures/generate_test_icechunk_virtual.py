"""Here we test generating icechunk virtual files"""

from virtualizarr import open_virtual_mfdataset
from virtualizarr.parsers import HDFParser
from virtualizarr.registry import ObjectStoreRegistry

import obstore
import icechunk

# NOTE: For now Ill build stores that are stored locally, but point to data on s3.
# Eventually this should probably be built out with a bunch of different options? Not sure if local storage referencing local files would make sense?

# Store that we cannot access from the tests (to ensure proper error handling) - MUR would fit the bill

# Store that points to a public s3 bucket (Using NLDAS as examples - see https://github.com/virtual-zarr/nldas-icechunk/tree/master for details)

urls = [
    "s3://nasa-waterinsight/NLDAS3/forcing/daily/200101/NLDAS_FOR0010_D.A20010101.030.beta.nc",
    "s3://nasa-waterinsight/NLDAS3/forcing/daily/200101/NLDAS_FOR0010_D.A20010102.030.beta.nc",
    "s3://nasa-waterinsight/NLDAS3/forcing/daily/200101/NLDAS_FOR0010_D.A20010103.030.beta.nc",
]

bucket = "s3://nasa-waterinsight"
store = obstore.store.from_url(bucket, region="us-west-2", skip_signature=True)
registry = ObjectStoreRegistry({bucket: store})
parser = HDFParser()

vds = open_virtual_mfdataset(
    urls,
    parser=parser,
    registry=registry,
)

storage = icechunk.local_filesystem_storage(
    "tests/fixtures/icechunk_virtual_accessible"
)

config = icechunk.RepositoryConfig.default()
config.set_virtual_chunk_container(
    icechunk.VirtualChunkContainer(
        "s3://nasa-waterinsight/NLDAS3/forcing/daily/",
        icechunk.s3_store(region="us-west-2"),
    )
)

virtual_credentials = icechunk.containers_credentials(
    {
        "s3://nasa-waterinsight/NLDAS3/forcing/daily/": icechunk.s3_anonymous_credentials()
    }
)

repo = icechunk.Repository.open_or_create(
    storage=storage,
    config=config,
    authorize_virtual_chunk_access=virtual_credentials,
)

session = repo.writable_session("main")
vds.vz.to_icechunk(session.store)
session.commit("Committed test dataset with virtual chunks")
print("Done committing virtual dataset with publicly accessible chunk to icechunk repo")
