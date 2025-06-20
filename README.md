**Project purposes**
  - Orchestrate multiple microservices with Docker Compose
  - MongoDB basics
  - Reliability concepts: Retry & Circuit-Breaker
  - Availability: Data Caching & Request Queuing
<br>
<br>
<br>

**System Setup**
<br>
<br>

*Supports 4 methods*
  + AddItem (POST): Duplicate 'id' and 'name' is not allowed
  + GetItem (GET): Search by 'id' gets single item, search by 'name' gets item stream (wrap around)
  + UpdateItem (PUT): Change name (no duplicate) of an item by 'id'
  + DeleteItem (DELETE): Remove item by 'id'
<br>
<br>

*REST-service*
- Provides human response to CURL request

- Uses FastAPI, continue from Lab 2

- Retry & Circuit-Breaker
  + 2 retries after 1 sec & 2 sec to avoid IO display issue with minimal time
  + Reset timeout is 6 sec for testing

- Supports: Request Queuing
  + When gRPC-service or MongoDB down, requests are put in a queue and will be processed when services are healthy again. This is done with a process checking the availability of required services by sending dummy request periodically in the background. 
<br>
<br>

*gRPC-service*
- Rule check for methods
- Data Caching of MongoDB using python dictionary
<br>
<br>

*MongoDB*
- Data is stored locally using Docker Volume
<br>
<br>
<br>

**How To Run**
<br>
<br>
Simply ```docker compose up -d```
<br>
<br>
<br>

Some sample curl commands to use the system
<br>
```curl -X POST -H "Content-Type: application/json" -d '{"id": 301, "name": "Test Item Reliability"}' "http://localhost:5000/items" ; echo'```
<br>
```curl -X GET "http://localhost:5000/items/?item_id=201" ; echo```
<br>
```curl -X GET "http://localhost:5000/items/?name=mouse" ; echo```
<br>
```curl -X PUT -H "Content-Type: application/json" -d '{"name": "Vertical Mouse"}' "http://localhost:5000/items/202" ; echo```
<br>
```curl -X DELETE "http://localhost:5000/items/200" ; echo```

