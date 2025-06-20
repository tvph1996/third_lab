import os
import logging
import grpc
from fastapi import FastAPI, HTTPException, Request, Response # type: ignore
import uvicorn # type: ignore
import myitems_pb2
import myitems_pb2_grpc
from pybreaker import CircuitBreaker, CircuitBreakerError # type: ignore
import asyncio
import json
import warnings


warnings.filterwarnings("ignore", category=DeprecationWarning)


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = FastAPI()


# gRPC Setup
GRPC_HOST = os.getenv("GRPC_HOST", "localhost")
GRPC_PORT = os.getenv("GRPC_PORT", "50051")
GRPC_ADDRESS = f"{GRPC_HOST}:{GRPC_PORT}"

gRPC_channel = grpc.insecure_channel(GRPC_ADDRESS)
gRPC_methods = myitems_pb2_grpc.ItemServiceStub(gRPC_channel)


# Circuit Breaker Setup
breaker = CircuitBreaker(fail_max=3, reset_timeout=6)
MAX_RETRIES = 2


# --- FastAPI REST-server Setup ---

@app.post("/items")
async def add_item(request: Request):

    global gRPC_channel, gRPC_methods
    delay = 1
    body = await request.json()
    item_id = body.get("id")
    name = body.get("name")
    
    if not all([isinstance(item_id, int), name]):
        raise HTTPException(status_code=400, detail="Request must include 'id' and 'name'.")

    grpc_request = myitems_pb2.Item(id=item_id, name=name) # type: ignore

    for attempt in range(MAX_RETRIES + 1):

        try:
            
            if breaker.current_state == 'CLOSED':
                
                response = breaker.call(gRPC_methods.AddItem, grpc_request, timeout=1.0)
            
            else:
                
                # Only create new channel when CircuitBreaker is HALF_OPEN
                gRPC_new_channel = grpc.insecure_channel(GRPC_ADDRESS)
                gRPC_methods = myitems_pb2_grpc.ItemServiceStub(gRPC_new_channel)
                response = breaker.call(gRPC_methods.AddItem, grpc_request, timeout=1.0)
            
            
            if response.result:
                
                content = {"message": "Item added successfully.", "item": {"id": response.added_item.id, "name": response.added_item.name}}
                return Response(content=json.dumps(content) + "\n", status_code=201, media_type="application/json")

            else:
                raise HTTPException(status_code=409, detail="Item with ID or name already exists.")
                

        except (CircuitBreakerError):

            content = {
                "status": "error",
                "message": "Service unavailable. The circuit breaker is open. Please try again later."
            }
            logging.warning(f"gRPC-service unavailable. Calls will no longer be accepted. Please try again later. {body}")
            return Response(content=json.dumps(content) + "\n", status_code=503, media_type="application/json")           


        except grpc.RpcError as err:
            
            logging.warning(f"Call {attempt + 1} failed")
            await asyncio.sleep(delay)
            delay *= 2
    
    


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



# Startup

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000)
