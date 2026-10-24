"""Builds a deterministic Northwind-small style SQLite fixture with NO declared
foreign keys (the shape that triggered invalid_join_relationship)."""
import random, sqlite3, sys
from datetime import date, timedelta

def build(path):
    rnd = random.Random(42)
    con = sqlite3.connect(path)
    c = con.cursor()
    c.executescript('''
    CREATE TABLE Category(Id INTEGER PRIMARY KEY, CategoryName TEXT, Description TEXT);
    CREATE TABLE Supplier(Id INTEGER PRIMARY KEY, CompanyName TEXT, Country TEXT);
    CREATE TABLE Shipper(Id INTEGER PRIMARY KEY, CompanyName TEXT);
    CREATE TABLE Employee(Id INTEGER PRIMARY KEY, LastName TEXT, FirstName TEXT, Title TEXT);
    CREATE TABLE Customer(Id TEXT PRIMARY KEY, CompanyName TEXT, Country TEXT, City TEXT);
    CREATE TABLE Product(Id INTEGER PRIMARY KEY, ProductName TEXT, SupplierId INTEGER, CategoryId INTEGER, UnitPrice REAL, UnitsInStock INTEGER, Discontinued INTEGER);
    CREATE TABLE "Order"(Id INTEGER PRIMARY KEY, CustomerId TEXT, EmployeeId INTEGER, OrderDate TEXT, ShipVia INTEGER, Freight REAL, ShipCountry TEXT);
    CREATE TABLE OrderDetail(Id TEXT PRIMARY KEY, OrderId INTEGER, ProductId INTEGER, UnitPrice REAL, Quantity INTEGER, Discount REAL);
    ''')
    cats = ["Beverages","Condiments","Confections","Dairy Products","Grains/Cereals","Meat/Poultry","Produce","Seafood"]
    c.executemany("INSERT INTO Category VALUES(?,?,?)", [(i+1,n,n+" desc") for i,n in enumerate(cats)])
    countries = ["USA","Germany","France","UK","Brazil","Canada","Japan"]
    c.executemany("INSERT INTO Supplier VALUES(?,?,?)", [(i+1,f"Supplier {i+1}",countries[i%7]) for i in range(10)])
    c.executemany("INSERT INTO Shipper VALUES(?,?)", [(1,"Speedy Express"),(2,"United Package"),(3,"Federal Shipping")])
    c.executemany("INSERT INTO Employee VALUES(?,?,?,?)", [(i+1,f"Last{i+1}",f"First{i+1}","Sales Rep") for i in range(6)])
    c.executemany("INSERT INTO Customer VALUES(?,?,?,?)", [(f"C{i:03d}",f"Customer {i}",countries[i%7],f"City{i%5}") for i in range(1,21)])
    c.executemany("INSERT INTO Product VALUES(?,?,?,?,?,?,?)", [(i+1,f"Product {i+1}",rnd.randint(1,10),rnd.randint(1,8),round(rnd.uniform(5,80),2),rnd.randint(0,100),rnd.randint(0,1)) for i in range(40)])
    start = date(2023,1,1)
    orders=[]; dets=[]
    for oid in range(1,201):
        d = start + timedelta(days=rnd.randint(0,364))
        orders.append((oid,f"C{rnd.randint(1,20):03d}",rnd.randint(1,6),d.isoformat(),rnd.randint(1,3),round(rnd.uniform(1,90),2),countries[rnd.randint(0,6)]))
        for pid in rnd.sample(range(1,41), rnd.randint(1,4)):
            dets.append((f"{oid}/{pid}",oid,pid,round(rnd.uniform(5,80),2),rnd.randint(1,30),rnd.choice([0,0,0.05,0.1,0.15])))
    c.executemany('INSERT INTO "Order" VALUES(?,?,?,?,?,?,?)', orders)
    c.executemany("INSERT INTO OrderDetail VALUES(?,?,?,?,?,?)", dets)
    con.commit(); con.close()

if __name__ == "__main__":
    build(sys.argv[1])
