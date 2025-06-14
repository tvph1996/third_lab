import os
import logging
import grpc
from fastapi import FastAPI, HTTPException, Request
import myitems_pb2
import myitems_pb2_grpc
from pybreaker import CircuitBreaker, CircuitBreakerError
import asyncio

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = FastAPI(title="REST-gRPC Chaining Service")



# --- gRPC Connection Setup ---
GRPC_HOST = os.getenv("GRPC_HOST", "localhost")
GRPC_PORT = os.getenv("GRPC_PORT", "50051")

breaker = CircuitBreaker(fail_max=3, reset_timeout=6)
MAX_RETRIES = 2

# global channel for other method except POST               
grpc_stub = myitems_pb2_grpc.ItemServiceStub(grpc.insecure_channel(f"{GRPC_HOST}:{GRPC_PORT}"))


# --- FastAPI Rest-Server Logic ---

@app.post("/items", status_code=201)
async def add_item(request: Request):

    delay = 1 # 1000ms instead of 100ms for stdio display better
    body = await request.json()
    item_id = body.get("id")
    name = body.get("name")

    grpc_request = myitems_pb2.Item(id=item_id, name=name)

    for attempt in range(MAX_RETRIES + 1):
        
        try:
            
            # start new gRPC connection after each Retry
            with grpc.insecure_channel(f"{GRPC_HOST}:{GRPC_PORT}") as channel:
                
                stub = myitems_pb2_grpc.ItemServiceStub(channel)
                
                response = breaker.call(stub.AddItem, grpc_request, timeout=1.0)
                
                if response.result:
                    return {"message": "Item added successfully.", "item": {"id": response.added_item.id, "name": response.added_item.name}}           
                else:
                    raise grpc.RpcError(grpc.StatusCode.UNKNOWN, "Unknown error in gRPC-service.")


        except CircuitBreakerError:
            logging.error("Circuit breaker is open. Not accept new calls.")
            raise HTTPException(status_code=503, detail="Service unavailable. Please try again later.")
        
        
        except grpc.RpcError as e:
            logging.warning(f"Call {attempt + 1} failed: {e.details()}")

            # Duplicate item check, Retry not needed
            if e.code() == grpc.StatusCode.ALREADY_EXISTS:
                logging.error("Duplicate error, no retry, exited")
                raise HTTPException(status_code=409, detail=e.details())
            
            if attempt >= MAX_RETRIES:
                break
            
            await asyncio.sleep(delay)
            delay *= 2 



@app.get("/items/")
def get_items(item_id: int = 0, name: str = ""):

    try:
        
        request = myitems_pb2.Item(id=item_id, name=name)

        responses = grpc_stub.GetItem(request, timeout=2.0)
        
        results = [{"id": resp.requested_item.id, "name": resp.requested_item.name} for resp in responses if resp.result]

        if not results:
            raise HTTPException(status_code=404, detail="No items found.")
        
        return {"message": "Items retrieved successfully.", "items": results}
    
    # Failure raised immediately
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

        item_to_update = myitems_pb2.Item(id=item_id, name=new_name)
        response = grpc_stub.UpdateItem(item_to_update, timeout=2.0)
        
        if response.result:
            return {
                "message": f"Item {item_id} updated successfully.",
                "old_item": {"id": response.old_item.id, "name": response.old_item.name},
                "new_item": {"id": response.new_item.id, "name": response.new_item.name},
            }
    
    # Failure raised immediately
    except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail=e.details())
        elif e.code() == grpc.StatusCode.ALREADY_EXISTS:
            raise HTTPException(status_code=409, detail=e.details())
        else:
            raise HTTPException(status_code=500, detail=f"gRPC backend failure: {e.details()}")



@app.delete("/items/{item_id}", status_code=200)
def delete_item(item_id: int):

    try:
        request = myitems_pb2.Item(id=item_id)
        response = grpc_stub.DeleteItem(request, timeout=2.0)

        if response.result:
            return {
                "message": f"Successfully deleted item.",
                "deleted_item": {"id": response.deleted_item.id, "name": response.deleted_item.name}
            }

    # Failure raised immediately
    except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail=e.details())
        else:
            raise HTTPException(status_code=500, detail=f"gRPC-service failure: {e.details()}")




if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)