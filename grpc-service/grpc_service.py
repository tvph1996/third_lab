import grpc
from concurrent import futures
import os
import logging
import re
from pymongo import MongoClient, errors
import myitems_pb2
import myitems_pb2_grpc
from collections import OrderedDict


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# Cache to hold up to 1000 items
CACHE_MAX_SIZE = 1000
item_cache = OrderedDict()


# --- MongoDB Connection ---
try:

    # one connection for all methods
    mongo_host = os.environ.get("MONGO_HOST", "localhost")
    mongo_port = int(os.environ.get("MONGO_PORT", 27017))
    mongo_db_name = os.environ.get("MONGO_DB", "itemsdb")
    
    client = MongoClient(mongo_host, mongo_port, serverSelectionTimeoutMS=5000)
    db = client[mongo_db_name]
    items_collection = db["items"]
    
    items_collection.create_index("id", unique=True)
    items_collection.create_index("name", unique=True) # name is also indexed for faster search and duplicate check
    logging.info("MongoDB connection initialized.")


except Exception as e:
    
    logging.error(f"Could not connect to MongoDB: {e}")
    exit(1)




# --- gRPC Services ---
class ItemServiceServicer(myitems_pb2_grpc.ItemServiceServicer):


    def AddItem(self, request, context):   
        
        logging.info(f"Request to add item: id={request.id}, name='{request.name}'")

        try:

            if items_collection.find_one({"$or": [{"id": request.id}, {"name": request.name}]}):
                logging.info(f"Item with id {request.id} or name '{request.name}' already exists.")
                context.set_details(f"Item with id {request.id} or name '{request.name}' already exists.")
                
                # context.set_code(grpc.StatusCode.ALREADY_EXISTS)
                # still return OK, only with result = False
                return myitems_pb2.AddItemResponse(result=False, added_item=request)
                
            items_collection.insert_one({"id": request.id, "name": request.name})
            logging.info(f"Added item id={request.id}, name='{request.name}'.")
            
            if len(item_cache) >= CACHE_MAX_SIZE:
                item_cache.popitem(last=False)
            
            item_cache[request.id] = request
            
            return myitems_pb2.AddItemResponse(result=True, added_item=request)

 
        except errors.ConnectionFailure as e:
            
            logging.error(f"MongoDB connection error in AddItem: {e}")
            context.set_details("MongoDB is currently unavailable.")
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            return myitems_pb2.AddItemResponse(result=False)



    def GetItem(self, request, context):
        
        # --- Search in Cache first ---
        cache_hits = []
        
        # Search by ID
        if request.id > 0 and request.id in item_cache:
            logging.info(f"Cache hit for item id: {request.id}")
            cache_hits.append(item_cache[request.id])
        
        # Search cache by name
        elif request.name:
            logging.info(f"Searching cache for name like: '{request.name}'")
            search_regex = re.compile(re.escape(request.name), re.IGNORECASE)
            for item in item_cache.values():
                if search_regex.search(item.name):
                    cache_hits.append(item)
        
        # Found item in cache -> return from Cache
        if cache_hits:
            logging.info(f"Found {len(cache_hits)} item(s) in cache.")
            for item in cache_hits:
                yield myitems_pb2.GetItemResponse(result=True, requested_item=item)
            return

        # --- Search in MongoDB when no result in Cache  ---
        logging.info("No item in Cache, continue to MongoDB.")

        query = {}

        if request.id > 0:
            query = {"id": request.id}
        elif request.name:
            query = {"name": re.compile(re.escape(request.name), re.IGNORECASE)}
        else:
            logging.warning("GetItem request received without a valid ID or name.")
            context.set_details("Provide a valid item ID (greater than 0) or a name to search.")
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            yield myitems_pb2.GetItemResponse(result=False)
            return

        try:

            found_items = items_collection.find(query)
            db_has_results = False

            for doc in found_items:
                
                db_has_results = True
                item_proto = myitems_pb2.Item(id=doc["id"], name=doc["name"])

                # Update cache with new data from DB
                if len(item_cache) >= CACHE_MAX_SIZE:
                    item_cache.popitem(last=False)
                item_cache[item_proto.id] = item_proto

                yield myitems_pb2.GetItemResponse(result=True, requested_item=item_proto)

            if not db_has_results:
                context.set_details("No items found in database.")
                context.set_code(grpc.StatusCode.NOT_FOUND)
                yield myitems_pb2.GetItemResponse(result=False)


        except errors.ConnectionFailure as e:
            
            logging.error(f"MongoDB error in GetItem: {e}")
            
            context.set_details("MongoDB is unavailable and item not in cache.")
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            yield myitems_pb2.GetItemResponse(result=False)
            return



    def UpdateItem(self, request, context):

        logging.info(f"Request to update item id={request.id} to name='{request.name}'")
        old_doc = items_collection.find_one({"id": request.id})
        
        if not old_doc:

            logging.info(f"Item with id {request.id} not found.")
            context.set_details(f"Item with id {request.id} not found.")
            context.set_code(grpc.StatusCode.NOT_FOUND)
            return myitems_pb2.UpdateItemResponse(result=False)

        if old_doc["name"] != request.name and items_collection.find_one({"name": request.name}):

            logging.info(f"Item name '{request.name}' is already in use.")
            context.set_details(f"Item name '{request.name}' is already in use.")
            context.set_code(grpc.StatusCode.ALREADY_EXISTS)
            return myitems_pb2.UpdateItemResponse(result=False)

        items_collection.update_one({"id": request.id}, {"$set": {"name": request.name}})

        logging.info(f"Updated item id={request.id}.")

        return myitems_pb2.UpdateItemResponse(
            result=True,
            old_item=myitems_pb2.Item(id=old_doc["id"], name=old_doc["name"]),
            new_item=request
        )



    def DeleteItem(self, request, context):

        logging.info(f"Request to delete item id: {request.id}")
        deleted_doc = items_collection.find_one_and_delete({"id": request.id})

        if deleted_doc:

            logging.info(f"Deleted item id={deleted_doc['id']}.")
            deleted_item = myitems_pb2.Item(id=deleted_doc["id"], name=deleted_doc["name"])
            return myitems_pb2.DeleteItemResponse(result=True, deleted_item=deleted_item)


        else:

            logging.info(f"Item with id {request.id} not found.")
            context.set_details(f"Item with id {request.id} not found.")
            context.set_code(grpc.StatusCode.NOT_FOUND)
            return myitems_pb2.DeleteItemResponse(result=False)




# --- gRPC Server Run ---
def serve():

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    myitems_pb2_grpc.add_ItemServiceServicer_to_server(ItemServiceServicer(), server)
    port = os.environ.get("GRPC_PORT", "50051")
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    logging.info(f"gRPC server listening on port {port}.")
    server.wait_for_termination()



if __name__ == '__main__':
    serve()

