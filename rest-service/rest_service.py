import os
import logging
import grpc
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
import myitems_pb2
import myitems_pb2_grpc
from pybreaker import CircuitBreaker, CircuitBreakerError
import asyncio
from collections import deque
import json
import warnings


warnings.filterwarnings("ignore", category=DeprecationWarning)


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = FastAPI()


# gRPC Setup
GRPC_HOST = os.getenv("GRPC_HOST", "localhost")
GRPC_PORT = os.getenv("GRPC_PORT", "50051")
GRPC_ADDRESS = f"{GRPC_HOST}:{GRPC_PORT}"


# Circuit Breaker Setup
breaker = CircuitBreaker(fail_max=3, reset_timeout=6)
MAX_RETRIES = 2


# Queue Setup
FAILED_REQUESTS_QUEUE = deque(maxlen=100)


# --- Queue controller running in background ---

async def process_queue():

    logging.info(f"Attempting to process {len(FAILED_REQUESTS_QUEUE)} items from the queue.")
    
    items_to_retry = list(FAILED_REQUESTS_QUEUE)
    FAILED_REQUESTS_QUEUE.clear()

    for item_body in items_to_retry:

        try:

            grpc_request = myitems_pb2.Item(id=item_body.get("id"), name=item_body.get("name"))
            with grpc.insecure_channel(GRPC_ADDRESS) as channel:
                stub = myitems_pb2_grpc.ItemServiceStub(channel)
                stub.AddItem(grpc_request, timeout=2.0)
            logging.info(f"Successfully processed queued item: {item_body}")

        except Exception as e:

            logging.error(f"Failed to process queued item {item_body}, re-queuing. Error: {e}")
            FAILED_REQUESTS_QUEUE.append(item_body)


async def check_grpc_health():

    try:

        with grpc.insecure_channel(GRPC_ADDRESS) as channel:
            stub = myitems_pb2_grpc.ItemServiceStub(channel)
            # Use a non-existent id -> expect NOT_FOUND
            request = myitems_pb2.Item(id=999999999) 
            for _ in stub.GetItem(request, timeout=1.0):
                pass
            return False 

    except grpc.RpcError as e:
 
        if e.code() == grpc.StatusCode.NOT_FOUND:
            return True 

        return False


    except Exception:
        return False


async def retry_queue_periodically():

    while True:
        await asyncio.sleep(15)
        
        if FAILED_REQUESTS_QUEUE:

            logging.info("Queue contains items. Checking gRPC-service health...")

            if await check_grpc_health():
                logging.info("gRPC service is healthy. Processing queued items.")
                await process_queue()

            else:
                logging.info("gRPC service still unhealthy. Retry later.")


@app.on_event("startup")
async def startup_event():
    logging.info("Starting background queue controller...")
    asyncio.create_task(retry_queue_periodically())



# --- FastAPI REST-server Setup ---

@app.post("/items")
async def add_item(request: Request):

    delay = 1
    body = await request.json()
    item_id = body.get("id")
    name = body.get("name")
    
    if not all([isinstance(item_id, int), name]):
        raise HTTPException(status_code=400, detail="Request must include 'id' and 'name'.")

    grpc_request = myitems_pb2.Item(id=item_id, name=name)

    try:

        for attempt in range(MAX_RETRIES + 1):

            try:

                with grpc.insecure_channel(GRPC_ADDRESS) as channel:
                    stub = myitems_pb2_grpc.ItemServiceStub(channel)
                    response = breaker.call(stub.AddItem, grpc_request, timeout=1.0)
                
                if response.result:
                    content = {"message": "Item added successfully.", "item": {"id": response.added_item.id, "name": response.added_item.name}}
                    return Response(content=json.dumps(content) + "\n", status_code=201, media_type="application/json")

                else:
                    raise grpc.RpcError(grpc.StatusCode.ALREADY_EXISTS, "Item with ID or name already exists.")
            

            except grpc.RpcError as e:

                if e.code() == grpc.StatusCode.ALREADY_EXISTS:
                    raise HTTPException(status_code=409, detail=e.details())

                if attempt >= MAX_RETRIES:
                    raise e # Re-raise final error to be caught by the outer block
 
                await asyncio.sleep(delay)
                delay *= 2
    

    except (CircuitBreakerError, grpc.RpcError):
        logging.warning(f"gRPC-service unavailable. Queuing item: {body}")
        FAILED_REQUESTS_QUEUE.append(body)
        content = {"message": "The service is temporarily unavailable. Request has been queued and will be processed automatically later."}
        return Response(content=json.dumps(content) + "\n", status_code=202, media_type="application/json")


@app.get("/items/")
def get_items(item_id: int = 0, name: str = ""):

    try:

        if not item_id and not name:
            raise HTTPException(status_code=400, detail="Provide 'id' or 'name'.")

        with grpc.insecure_channel(GRPC_ADDRESS) as channel:
            stub = myitems_pb2_grpc.ItemServiceStub(channel)
            request = myitems_pb2.Item(id=item_id, name=name)
            responses = stub.GetItem(request, timeout=2.0)
            results = [{"id": resp.requested_item.id, "name": resp.requested_item.name} for resp in responses if resp.result]

            if not results:
                raise HTTPException(status_code=404, detail="No items found.")

            return {"message": "Items retrieved successfully.", "items": results}


    except grpc.RpcError as e:

        if e.code() == grpc.StatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail=e.details())

        else:
            raise HTTPException(status_code=500, detail=f"gRPC-service failure: {e.details()}")


@app.put("/items/{item_id}")
async def update_item(item_id: int, request: Request):

    try:
        body = await request.json()
        new_name = body.get("name")

        if not new_name:
            raise HTTPException(status_code=400, detail="The 'name' field is required in the request body.")

        with grpc.insecure_channel(GRPC_ADDRESS) as channel:
            stub = myitems_pb2_grpc.ItemServiceStub(channel)
            item_to_update = myitems_pb2.Item(id=item_id, name=new_name)
            response = stub.UpdateItem(item_to_update, timeout=2.0)

            if response.result:
                return {"message": f"Item {item_id} updated successfully.", "old_item": {"id": response.old_item.id, "name": response.old_item.name}, "new_item": {"id": response.new_item.id, "name": response.new_item.name}}


    except grpc.RpcError as e:

        if e.code() == grpc.StatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail=e.details())

        elif e.code() == grpc.StatusCode.ALREADY_EXISTS:
            raise HTTPException(status_code=409, detail=e.details())

        else:
            raise HTTPException(status_code=500, detail=f"gRPC-service failure: {e.details()}")


@app.delete("/items/{item_id}", status_code=200)
def delete_item(item_id: int):
    try:

        with grpc.insecure_channel(GRPC_ADDRESS) as channel:
            stub = myitems_pb2_grpc.ItemServiceStub(channel)
            request = myitems_pb2.Item(id=item_id)
            response = stub.DeleteItem(request, timeout=2.0)

            if response.result:
                return {"message": "Successfully deleted item.", "deleted_item": {"id": response.deleted_item.id, "name": response.deleted_item.name}}


    except grpc.RpcError as e:

        if e.code() == grpc.StatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail=e.details())

        else:
            raise HTTPException(status_code=500, detail=f"gRPC-service failure: {e.details()}")




if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
