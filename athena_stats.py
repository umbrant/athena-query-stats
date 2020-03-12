import io
import gzip
import json
import uuid
import argparse
from datetime import datetime, date
from queue import Queue
import threading
import os

import boto3
from botocore.config import Config

config = Config(
    retries=dict(
        max_attempts=10
    )
)

athena_client = boto3.client('athena', config=config)
s3_client = boto3.client('s3', config=config)

# 50 is the max query execution details we can fetch in a batch
MAX_ATHENA_BATCH_SIZE = 50

q = Queue()
num_processed = 0


def parse_args():
    """Command line argument parser"""
    parser = argparse.ArgumentParser(description='Extract Athena query execution data to S3.')
    parser.add_argument("bucket", help="The S3 bucket name to upload query executions to")
    parser.add_argument("prefix", help="The prefix in which to store uploaded query executions")
    return parser.parse_args()


def process_batch(execution_ids, bucket, prefix):
    stats = get_query_executions(execution_ids)
    upload_to_s3(stats, bucket, prefix)


def do_work(bucket, prefix):
    global q, num_processed
    while True:
        execution_ids = q.get()
        if execution_ids:
            process_batch(execution_ids, bucket, prefix)
            q.task_done()
            num_processed += len(execution_ids)
            print(f'Processed batch with {len(execution_ids)} ({num_processed} total)')


def loop_and_fetch_stats(bucket, prefix):
    global q

    max_threads = 25
    threads = []
    for x in range(max_threads):
        threads.append(threading.Thread(target=do_work, args=(bucket, prefix), daemon=True))
    for thread in threads:
        thread.start()

    execution_ids = []
    counter = 1
    for qid in get_execution_ids():
        execution_ids.append(qid)
        # If we've collected fifty, go ahead and fetch those stats and upload them back to S3
        if MAX_ATHENA_BATCH_SIZE == len(execution_ids):
            print("Adding batch %d (%d total queries) to queue" % (counter, counter * MAX_ATHENA_BATCH_SIZE))
            q.put(execution_ids)
            execution_ids = []
            counter += 1

    # All done fetching execution ids, if we have any left, upload the stats
    if execution_ids:
        print("Adding final batch %d (%d total queries)" % (
            counter + 1,
            (counter * MAX_ATHENA_BATCH_SIZE) + len(execution_ids)
        ))
        q.put(execution_ids)
        stats = get_query_executions(execution_ids)
        upload_to_s3(stats, bucket, prefix)

    q.join()


def upload_to_s3(query_stats, bucket, prefix):
    # We'll do this all in memory
    # Create a gzip'ed JSON bytes object
    # Useful :) https://gist.github.com/veselosky/9427faa38cee75cd8e27
    writer = io.BytesIO()
    gzip_out = gzip.GzipFile(fileobj=writer, mode='w')
    for record in query_stats:
        # print(json.dumps(record, default=json_serial))
        # json.dump(record, gzip_out, default=json_serial)
        json_line = json.dumps(record, default=json_serial) + "\n"
        gzip_out.write(json_line.encode())
    gzip_out.close()

    # s3_client.upload_fileobj(writer, TARGET_BUCKET, TARGET_PREFIX + "damon.json.gz")
    key = os.path.join(prefix, str(uuid.uuid4()) + '.json.gz')
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        ContentType='text/plain',  # the original type
        ContentEncoding='gzip',  # MUST have or browsers will error
        Body=writer.getvalue()
    )


def get_query_executions(ids):
    """Retrieve details on the provided query execuution IDs"""
    response = athena_client.batch_get_query_execution(
        QueryExecutionIds=ids
    )
    return response['QueryExecutions']


def get_execution_ids():
    """Retrieve the list of all executions from the Athena API"""
    query_params = {}  # Empty dictionary for next token
    while True:
        response = athena_client.list_query_executions(**query_params)
        for execution_id in response['QueryExecutionIds']:
            yield execution_id

        if 'NextToken' in response:
            query_params['NextToken'] = response['NextToken']
        else:
            break


def json_serial(obj):
    """JSON serializer for datetime objects not serializable by default JSON code"""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError("Type %s not serializable" % type(obj))


if __name__ == "__main__":
    args = parse_args()
    loop_and_fetch_stats(args.bucket, args.prefix)
