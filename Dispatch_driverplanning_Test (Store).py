import streamlit as st
import pandas as pd
import folium
import io
from folium.plugins import MarkerCluster
from streamlit_folium import st_folium
from geopy.distance import geodesic
from datetime import timedelta
from ortools.constraint_solver import routing_enums_pb2, pywrapcp

#------------------------------------------------------------------------------

st.set_page_config(layout="wide")
st.title("Driver Route Planner with ETA - Store Vesion")
# Footer note
st.markdown("<div style='text-align:right; font-size:12px; color:gray;'>Version 1.0.2 Developed by Jidapa Buranachan</div>", unsafe_allow_html=True)

#------------------------------------------------------------------------------

# Create template dataframe
Loc_template = pd.DataFrame(columns=["Order No", "LAT", "LON", "order_datetime"])

# ใช้ BytesIO สำหรับ .xlsx
excel_buffer = io.BytesIO()
with pd.ExcelWriter(excel_buffer, engine='xlsxwriter') as writer:
    Loc_template.to_excel(writer, index=False, sheet_name='OrderLocation')

# ปุ่มดาวน์โหลด .xlsx
st.download_button(
    label="⬇️ Download Order Location Template (.xlsx)",
    data=excel_buffer.getvalue(),
    file_name="OrderLocation_Template.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

#------------------------------------------------------------------------------

# Upload files
#order_file = st.file_uploader("Upload OrderList.xlsx", type=["xlsx"], key="order")
location_file = st.file_uploader("Upload OrderLocation.xlsx", type=["xlsx"], key="location")

#------------------------------------------------------------------------------

# Parameters
num_drivers = st.number_input("Number of Drivers", min_value=1, value=3, step=1)
max_drops_per_driver = st.number_input("Max Drops per Driver", min_value=1, value=2, step=1)
col1, col2 = st.columns(2)
with col1:
    depot_lat = st.number_input("Depot Latitude", format="%.8f")
with col2:
    depot_lon = st.number_input("Depot Longitude", format="%.8f")

depot = (depot_lat, depot_lon)

#------------------------------------------------------------------------------

if location_file:
    location_df = pd.read_excel(location_file)
    merged_df = location_df.drop_duplicates(subset=['Order No', 'LAT', 'LON'])
    
#----------------------------------------------------------------------------------------
    
    merged_df['order_datetime'] = pd.to_datetime(
        format='%d/%m/%Y %H:%M', errors='coerce'
    )
    merged_df['distance_km'] = merged_df.apply(
        lambda row: geodesic((row['LAT'], row['LON']), depot).km, axis=1
    )
    merged_df['zone'] = merged_df['distance_km'].apply(lambda d: 'sameday' if d <= 5 else 'nextday')
    merged_df['delivery_deadline'] = merged_df.apply(
        lambda row: row['order_datetime'] + timedelta(hours=3) if row['zone'] == 'sameday' else pd.NaT,
        axis=1
    )

#------------------------------------------------------------------------------

    df_zone = merged_df[merged_df['zone'] == 'sameday'].copy().reset_index(drop=True)
    locations = [depot] + list(zip(df_zone['LAT'], df_zone['LON']))
    distance_matrix = [[geodesic(a, b).km for b in locations] for a in locations]

    manager = pywrapcp.RoutingIndexManager(len(locations), num_drivers, 0)
    routing = pywrapcp.RoutingModel(manager)
    
#------------------------------------------------------------------------------
    
    def distance_callback(from_index, to_index):
        f = manager.IndexToNode(from_index)
        t = manager.IndexToNode(to_index)
        return int(distance_matrix[f][t] * 1000)

    transit_cb_idx = routing.RegisterTransitCallback(distance_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_cb_idx)

    # ✅ Add Distance dimension to limit each route to 5km (5000 meters)
    distance_dimension_name = "Distance"
    routing.AddDimension(
        transit_cb_idx,
        0,        # slack
        10000,     # max 5,000 meters per route
        True,     # start from 0
        distance_dimension_name
    )
    
    # (Optional) เข้าถึง object เพื่อ debug หรือดูค่าระยะในแต่ละ vehicle
    distance_dimension = routing.GetDimensionOrDie(distance_dimension_name)
    
#------------------------------------------------------------------------------
    
    def demand_callback(from_index):
        from_node = manager.IndexToNode(from_index)
        return 0 if from_node == 0 else 1

    demand_cb_idx = routing.RegisterUnaryTransitCallback(demand_callback)
    routing.AddDimensionWithVehicleCapacity(
        demand_cb_idx,
        0,
        [max_drops_per_driver] * num_drivers,
        True,
        "DropCount"
    )

    for vehicle_id in range(1, num_drivers):
        routing.SetFixedCostOfVehicle(1000 * vehicle_id, vehicle_id)

    search_params = pywrapcp.DefaultRoutingSearchParameters()
    search_params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC

    total_orders = len(df_zone)
    max_capacity = num_drivers * max_drops_per_driver

    if total_orders > max_capacity:
        st.error(f"❌ Orders ({total_orders}) exceed driver capacity ({max_capacity}).")
    else:
        solution = routing.SolveWithParameters(search_params)
        if not solution:
            st.error("❌ No routing solution found.")
        else:
            driver_results = []
            merged_df['Driver'] = None
            merged_df['Drop no.'] = None
            speed_kmph = 30

            for vehicle_id in range(num_drivers):
                index = routing.Start(vehicle_id)
                route_nodes = []

                while not routing.IsEnd(index):
                    node = manager.IndexToNode(index)
                    if node != 0:
                        route_nodes.append(node)
                    index = solution.Value(routing.NextVar(index))

                if not route_nodes:
                    driver_results.append((f"Driver {vehicle_id + 1}", []))
                    continue

                base_time = df_zone.iloc[route_nodes[0] - 1]['order_datetime']
                cumulative_time = timedelta()
                vehicle_eta = []

                index = routing.Start(vehicle_id)
                current_node = manager.IndexToNode(index)

                while not routing.IsEnd(index):
                    next_index = solution.Value(routing.NextVar(index))
                    next_node = manager.IndexToNode(next_index)

                    if next_node == 0 or routing.IsEnd(next_index):
                        break

                    dist = distance_matrix[current_node][next_node]
                    travel_time = timedelta(hours=dist / speed_kmph)
                    cumulative_time += travel_time
                    eta = (base_time + cumulative_time).strftime("%H:%M")

                    order_no = df_zone.iloc[next_node - 1]['Order No']
                    merged_df.loc[merged_df['Order No'] == order_no, 'ETA'] = eta
                    merged_df.loc[merged_df['Order No'] == order_no, 'Driver'] = f"Driver {vehicle_id + 1}"
                    merged_df.loc[merged_df['Order No'] == order_no, 'Drop no.'] = len(vehicle_eta) + 1
                    vehicle_eta.append((order_no, eta))

                    index = next_index
                    current_node = next_node

                driver_results.append((f"Driver {vehicle_id + 1}", vehicle_eta))

#------------------------------------------------------------------------------

            # คำนวณจำนวนลูกค้าในแต่ละโซน
            zone_counts = merged_df['zone'].value_counts().to_dict()
                            
            # เตรียมข้อความแสดงผล
            sameday_count = zone_counts.get('sameday', 0)
            nextday_count = zone_counts.get('nextday', 0)
                            
            st.markdown(f"""
            📦 **Customer Zone Summary**  
            - Sameday: {zone_counts.get('sameday', 0)} customers  
            - Nextday: {zone_counts.get('nextday', 0)} customers
            """)

#------------------------------------------------------------------------------

            # Map visualization 1
            m = folium.Map(location=depot, zoom_start=12)
            color_map = {'sameday': 'green', 'nextday': 'red'}
            
            # วาดจุดศูนย์กลาง
            folium.Marker(location=depot, popup='Depot', icon=folium.Icon(color='blue')).add_to(m)
            
            # วาดลูกค้า
            for _, row in merged_df.iterrows():
                folium.CircleMarker(
                    location=(row['LAT'], row['LON']),
                    radius=5,
                    color=color_map[row['zone']],
                    fill=True,
                    popup=f"Customer: {row['Order No']} | {row['zone']} | {row['distance_km']:.2f} km"
                ).add_to(m)
            
            # เพิ่มวงรัศมี 5 กม. (เส้นขอบโซน sameday)
            folium.Circle(location=depot, radius=5000, color='gray', fill=False).add_to(m)
            st_folium(m, width=1600, height=900)

#------------------------------------------------------------------------------
  
            summary_df = merged_df[[
                'Order No', 'LAT', 'LON', 'distance_km', 'zone',
                'order_datetime', 'delivery_deadline', 'Driver', 'Drop no.', 'ETA',
                'Picking Zone', 'Ambient', 'VM+01 C', '20 C', 'Frozen'
            ]]
            st.subheader("Routing Summary")
            st.dataframe(summary_df)

#------------------------------------------------------------------------------
            
            # Map visualization 2
            st.subheader("Route Map")
            route_map = folium.Map(location=depot, zoom_start=12)
            colors = ['red', 'blue', 'green', 'purple', 'orange', 'darkred']
            folium.Marker(depot, popup='Depot', icon=folium.Icon(color='black')).add_to(route_map)

            for vehicle_id in range(num_drivers):
                index = routing.Start(vehicle_id)
                route_coords = []
                while not routing.IsEnd(index):
                    node = manager.IndexToNode(index)
                    if node == 0:
                        route_coords.append(depot)
                    else:
                        lat, lon = df_zone.iloc[node - 1][['LAT', 'LON']]
                        route_coords.append((lat, lon))
                    index = solution.Value(routing.NextVar(index))
                route_coords.append(depot)

                folium.PolyLine(route_coords, color=colors[vehicle_id % len(colors)],
                                weight=5, opacity=0.8, popup=f"Driver {vehicle_id + 1}").add_to(route_map)

                for i, coord in enumerate(route_coords[1:-1], start=1):
                    folium.Marker(coord,
                                  icon=folium.Icon(color=colors[vehicle_id % len(colors)], icon='truck', prefix='fa'),
                                  popup=f"Driver {vehicle_id + 1} - Stop {i}").add_to(route_map)

            st_folium(route_map, width=1600, height=900)

#-------------------------------------------------------------------------


