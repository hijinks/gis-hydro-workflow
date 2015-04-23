# -*- coding: utf-8 -*-
"""
Created on Thu Jan 29 15:14:57 2015

@author: Sam Brooke
"""
import cement
import yaml
import os
from os.path import basename

import arcpy
import datetime
import shutil
import math
import csv
import glob
from arcpy import env
from arcpy.sa import *

from cement.core import foundation, controller
from cement.core.controller import expose
from cement.utils import shell

class GISAppController(controller.CementBaseController):
    class Meta:
        label = 'base'
        description = 'Python CLI application to automate some GIS processing'
        arguments = [
            ( ['-c', '--config'], dict(action='store', dest='config',
                      help='path to config file') ),
            ( ['-b', '--batch'], dict(action='store', dest='batch',
                      help='path to batch directory') ),
            ]

    @expose(hide=True, aliases=['run'])
    def default(self):
        print("Running in default mode")

    @expose(help='Use existing batch and skip to watershed processing')
    def process_watersheds(self):
        print("Skipping to watersheds")
        self.app.skip_to_watersheds = 1

    @expose(help='Use existing batch and skip to watershed processing')
    def calculate_bqart(self):
        print("Skipping to discharge calculations")
        self.app.skip_to_watersheds = 1
        self.app.skip_to_discharge = 1        
        
class GISApp(foundation.CementApp):
    
    skip_to_watersheds = 0
    skip_to_discharge = 0
    
    class Meta:
        label = 'GIS_Automator'
        base_controller = GISAppController

   
app = GISApp()

class GISbatch:
    'Common base class for GIS batch processing'
   
    def __init__(self, config, batch = False):
        self.project_root = config['root']
        self.project_name = config['project_name']
        self.projection_code = config['projection_code']
        self.pour_points_path = config['pour_points_path']
        self.fault_path = config['fault_path']
        self.fault_data = ''
        self.scratch_path = config['scratch']
        self.output_path = config['output']
        self.original_dem = config['original_dem']
        self.uplift_mm_yr = config['uplift_mm_yr']
        
        # Workflow variables
        self.flow_dir = config['flow_dir']
        self.flow_acc = config['flow_acc']
        self.str_net = config['str_net']
        self.set_null = config['set_null']
        self.str_ord = config['str_ord']
        self.faults = config['faults']
        self.pour_points = config['pour_points']
        
        
        # Climate variables
        self.climates = config['climates']

        # Fault data
        self.fault_meta_data = {}
        
        # Set the environment variables
        arcpy.env.scratchWorkspace = self.scratch_path
        self.set_environment(batch)

        sr = arcpy.SpatialReference(self.projection_code)
        arcpy.env.outputCoordinateSystem = sr

        # Load in Spatial Analyst Toolbox
        arcpy.CheckOutExtension("Spatial")
    
    
    def set_environment(self, batch = False):
        if batch:
            self.batch_path = batch
            arcpy.env.workspace = self.batch_path
        else:
            self.batch_path = self.set_workspace()
            arcpy.env.workspace = self.batch_path

    
    def get_time_string(self):
        t = datetime.datetime.now()
        tstuff = [t.year, t.month, t.day, t.hour, t.minute, t.second]
        # Convert integer values to string
        tstring_list = map(str, tstuff)
        d = '_'.join(tstring_list)
        
        return d
        
    def set_workspace(self):
        
        if app.pargs.batch is None:
            
            dirname = self.get_time_string()
        
            # Assuming it doesn't exist already
            output_batch_path = os.path.join(self.output_path, dirname)
  
            os.makedirs(output_batch_path)
            
            # Copy original DEM
            shutil.copy2(self.original_dem, output_batch_path)
            
        else:
            if os.path.isdir(app.pargs.batch):
                output_batch_path = app.pargs.batch
            else:
                print('Batch directory does not exist')
                exit
        
        return output_batch_path 
        

    def hydro_workflow(self):
        print('Starting Hydrology Workflow...')
      
        print('Fill')
        fill_path = self.fill()
        
        print('Flow direction')
        flow_path = self.flow_direction(fill_path)
        
        print('Flow accumulation')
        flow_acc_path = self.flow_accumulation(flow_path)
        
        print('Steam network')
        stream_net_path = self.stream_network(flow_acc_path)

        print('Nullify')
        null_path = self.nullify(stream_net_path)
        
        print('Stream order')
        s_ord_path = self.stream_order(null_path, flow_path)
        
        print('Vectorise streams')
        vector_streams = self.vectorise_streams(s_ord_path, flow_path)
        
        # Save file values to YAML file
        hydro_paths = {
            'fill_path' : fill_path,
            'flow_path' : flow_path,
            'flow_acc_path' : flow_acc_path,
            'stream_net_path' : stream_net_path,
            'null_path' : null_path,
            's_ord_path' : s_ord_path,
            'vector_streams' : vector_streams,
            'fault_data': '',
            'fault_data_meta':'',
            'uplift_rate':self.uplift_mm_yr
       }
       
        with open(os.path.join(self.batch_path,'hydro_paths.yml'), 'w') as outfile:
            outfile.write(yaml.dump(hydro_paths, default_flow_style=True) )
        
        return hydro_paths
        
    def fault_workflow(self, faultlines, hydro_paths):
        
        print('Starting Fault workflow')

        
        print('Extracting fault data')
        self.get_fault_data(faultlines)
        
        print(self.fault_meta_data)
        
        print('Find intersects of faults and streams')
        # Fault intersects
        intersects_multipart = self.fault_intersects(faultlines, hydro_paths['vector_streams'], self.faults['cluster_tolerance'])
        
        print('Changing intersects to singlepart dataset')
        # Multipart to singlepart
        intersects_singlepart = self.intersects_to_singlepart(intersects_multipart)
        
        print('Removing low lying areas')
        # Remove areas that are too low
        self.highlands = self.remove_lowlands(self.pour_points['minimum_height'])
        
        print('Removing pour point intersects below '+ str(self.pour_points['minimum_height']))
        # Extract pour points above minimum height 
        pour_points = self.ignore_lowest_pp(intersects_singlepart, self.highlands)
        
        print('Create fault routes')
        # Create fault routes
        fault_routes = self.fault_routes(faultlines)
        
        print('Measure pour points along faults')
        # Generate intersect events
        intersect_events = self.intersect_events(pour_points, fault_routes, self.faults['search_radius'])
        
        print('Saving fault data')
        self.fault_data = self.extract_intersect_positions(intersect_events)
        
        
        # Updating yaml paths
        
        hydro_paths['fault_data'] = self.fault_data
        hydro_paths['fault_meta_data'] = self.fault_meta_data
        
        with open(os.path.join(self.batch_path,'hydro_paths.yml'), 'w') as outfile:
            outfile.write(yaml.dump(hydro_paths, default_flow_style=True) )
        
        return pour_points
    
    
    def watershed_workflow(self, original_pour_points, hydro_paths):

        print('Starting Watershed workflow')
      
        print('Creating batch directory')
        self.watershed_batch_path, pp_path = self.setup_watershed_batch(original_pour_points)
        
        print('Snap to pour points')
        snap_pp_path = self.snap_pour_points(pp_path, hydro_paths['flow_acc_path'])

        print('Extract watersheds')
        ws_path = self.watersheds(hydro_paths['flow_path'], snap_pp_path)
        
        # print('Converting to polygons')
        # self.ws_to_poly(ws_path)

        
        watershed_paths = {
            'pour_points' : snap_pp_path,
            'watersheds' : ws_path
        }
        
        with open(os.path.join(self.watershed_batch_path,'watershed_paths.yml'), 'w') as outfile:
            outfile.write(yaml.dump(watershed_paths, default_flow_style=True) )
            
        return ws_path


    def bqart_workflow(self, watershed_raster, hydro_paths, watershed_path, 
                       temp_directory, precip_directory, climate_scenario):
        
        print('Starting BQART workflow')
        
        print('Creating climate batch directory')
        climate_batch_path = self.climate_batch_directory(watershed_path)
        climate_cache_path = os.path.join(watershed_path, 'climate_cache')
        
        precip_cache_check = self.check_climate_cache(watershed_path, climate_scenario, 't')
        temp_cache_check = self.check_climate_cache(watershed_path, climate_scenario, 'p')
        
        if precip_cache_check:
            print('Precipitation cache found')
            precip_clip = temp_cache_check
        else:
            print('Averaging precipitation rasters')
            datatype = 'p'
            combined_name = datatype + '_' + climate_scenario + '_all.tif'
            clip_name = datatype + '_' + climate_scenario + '_clip.tif'
            
            precip_raster_path = self.average_rasters(precip_directory, climate_cache_path, combined_name, 0)
            
            print('Clipping precipitation rasters')
            precip_clip = self.clip_raster(precip_raster_path, climate_cache_path, clip_name, watershed_raster)
            
        if temp_cache_check:
            print('Temperature cache found')
            temp_clip = temp_cache_check
        else:
            datatype = 't'
            combined_name = datatype + '_' + climate_scenario + '_all.tif'
            clip_name = datatype + '_' + climate_scenario + '_clip.tif'
            
            print('Averaging temperature rasters')   
            temp_raster_path = self.average_rasters(temp_directory, climate_cache_path, combined_name, 1)

            print('Clipping temperature rasters')
            temp_clip = self.clip_raster(temp_raster_path, climate_cache_path, clip_name, watershed_raster)
            
            
            
        print('Climate zone statistics') 
        tz_dat_path = self.zone_statistics(climate_batch_path, watershed_raster, temp_clip, 'temp_data')
        pz_dat_path = self.zone_statistics(climate_batch_path, watershed_raster, precip_clip, 'precip_data')
        ez_dat_path = self.zone_statistics(climate_batch_path, watershed_raster, self.original_dem, 'elev_data')
        
        print('Calculating Qs using BQART') 
        qs_data = self.do_bqart(pz_dat_path, tz_dat_path, ez_dat_path, 
            hydro_paths['fault_data'], hydro_paths['fault_meta_data'], 
            hydro_paths['uplift_rate'])
        
        self.save_data_to_csv(qs_data, climate_batch_path)
        
    # ARC GIS PROCESSES
    # Hydro stuff

    def fill(self):
        fill_z_limit = ""

        out_fill = Fill(self.original_dem, fill_z_limit)
        out_fill_raster = self.project_name + '_fill.tif'
        out_fill_path = os.path.join(self.batch_path, out_fill_raster)
        out_fill.save(out_fill_path)
        
        return out_fill_path
        

    def flow_direction(self, fill_path):
        force_flow = self.flow_dir['force_flow']
        
        out_flow_dir = FlowDirection(fill_path, force_flow)
        out_flow_dir_raster = self.project_name + '_f_dir.tif'
        out_flow_dir_path = os.path.join(self.batch_path, out_flow_dir_raster)
        out_flow_dir.save(out_flow_dir_path)
        
        return out_flow_dir_path
        

    def flow_accumulation(self, flow_path):
        flow_weight_raster = self.flow_acc['flow_weight_raster']
        flow_data_type = self.flow_acc['flow_data_type']

        out_flow_acc = FlowAccumulation(flow_path, flow_weight_raster, flow_data_type)
        out_flow_acc_raster = self.project_name + '_f_acc.tif'
        out_flow_acc_path = os.path.join(self.batch_path, out_flow_acc_raster)
        out_flow_acc.save(out_flow_acc_path)
        
        return out_flow_acc_path
        

    def stream_network(self, flow_acc_path):
        con_where_clause = self.str_net['conditional']
        false_constant = self.str_net['false_constant']
        true_raster = flow_acc_path

        out_con = Con(flow_acc_path, true_raster, false_constant, con_where_clause)
        out_con_raster = self.project_name + '_net.tif'
        stream_net_path = os.path.join(self.batch_path, out_con_raster)
        out_con.save(stream_net_path)
        
        return stream_net_path
        
 
    def nullify(self, stream_net_path):
        false_raster = self.set_null['false_raster']
        null_where_clause = self.set_null['conditional']

        out_null = SetNull(stream_net_path, false_raster, null_where_clause)
        out_null_raster = self.project_name + '_net_null.tif'
        out_null_path = os.path.join(self.batch_path, out_null_raster)
        out_null.save(out_null_path)
        
        return out_null_path       
        

    def stream_order(self, null_path, flow_path):
        method = self.str_ord['method']
        
        out_s_ord_raster = self.project_name + '_s_order.tif'
        out_s_ord_path = os.path.join(self.batch_path, out_s_ord_raster)
        out_s_ord = StreamOrder(null_path, flow_path, method)
        out_s_ord.save(out_s_ord_path)
        
        return out_s_ord_path
        

    def vectorise_streams(self, s_ord_path, flow_path):
        out_sf_name = self.project_name + '_streams.shp'
        out_sf_path = os.path.join(self.batch_path, out_sf_name)
        StreamToFeature(s_ord_path, flow_path, out_sf_path)
        
        return out_sf_path
        
        
    # Fault stuff
        
    def get_fault_data(self, faultlines):
        fc = arcpy.SearchCursor(faultlines)
        for row in fc:
            # Get mean temperature
            self.fault_meta_data.update({row.getValue('FID'):{
                        'name':row.getValue('name'), 
                        'slip_min':row.getValue('slip_min'),
                        'slip_max':row.getValue('slip_max'), 
                        'age_min':row.getValue('age_min'), 
                        'age_max':row.getValue('age_max'), 
                        'sense':row.getValue('sense')}})        
                        
        
    def fault_intersects(self, faultlines, streams, cluster_tolerance):
        inFeatures = [faultlines, streams]
        intersects_name = self.project_name + '_intersects_multipart.shp'
        intersects_multipart = os.path.join(self.batch_path, intersects_name)
        arcpy.Intersect_analysis(inFeatures, intersects_multipart, "", cluster_tolerance, "point")
        
        return intersects_multipart
        
        
    def intersects_to_singlepart(self, intersects_multipart):
        intersects_name = self.project_name + '_intersects_singlepart.shp'
        intersects_singlepart = os.path.join(self.batch_path, intersects_name)
        arcpy.MultipartToSinglepart_management(intersects_multipart, intersects_singlepart)
        
        return intersects_singlepart
    
    def remove_lowlands(self, minimum_height):
        extract = ExtractByAttributes(self.original_dem, "VALUE > "+str(minimum_height))
        dem_no_lowlands = os.path.join(self.batch_path, self.project_name + '_dem_no_lowlands.tif')
        extract.save(dem_no_lowlands)
        
        return dem_no_lowlands

    def ignore_lowest_pp(self, intersects_singlepart, dem_no_lowlands):
        intersect_heights = os.path.join(self.batch_path, self.project_name + '_intersects_all.shp')
        ExtractValuesToPoints(intersects_singlepart, dem_no_lowlands, intersect_heights,
                      "INTERPOLATE", "ALL") 
              
        intersect_heights_above = os.path.join(self.batch_path,  self.project_name + '_intersects_above.shp')
        arcpy.Select_analysis(intersect_heights, intersect_heights_above, '"RASTERVALU" > 0')
        
        return intersect_heights_above
        

    def fault_routes(self, faultlines):
        fault_routes = os.path.join(self.batch_path, self.project_name + "_fault_routes.shp")
        arcpy.CreateRoutes_lr(faultlines, 'Id', fault_routes, "LENGTH")
        
        return fault_routes
        
        
    def intersect_events(self, pour_points, fault_routes, search_radius):
        intersect_events = os.path.join(self.batch_path, self.project_name + "_intersect_events.dbf")
        arcpy.LocateFeaturesAlongRoutes_lr(pour_points, fault_routes, "Id", search_radius, intersect_events, "RID POINT MEAS")
        
        return intersect_events
        
        
    def extract_intersect_positions(self, intersect_events):
        ic_cursor = arcpy.SearchCursor(intersect_events)
        fieldnames = [field.name for field in arcpy.ListFields(intersect_events)]
        ic_data = []
        for row in ic_cursor:
            # Get mean temperature
            ic_data.append([row.getValue('OID'), row.getValue(fieldnames[4]), row.getValue('MEAS')])
        
        intersect_data = os.path.join(self.batch_path, self.project_name + "_intersect_data.csv")
        row_headers = ['id', 'fault', 'distance']
        with open(intersect_data, 'wb') as qs_file:
            a = csv.writer(qs_file, delimiter=',')
            a.writerow(row_headers)
            for r in ic_data:
                a.writerow([r[0], r[1], r[2]])
                
        # Unlock data
        del row 
        del ic_cursor
        
        return intersect_data
    
    
    
    # Watershed stuff
    
    def setup_watershed_batch(self, original_pour_points):
        # Each watershed calculations need to be discrete from one another
        timestamp = self.get_time_string()
        
        if os.path.isdir(os.path.join(self.batch_path, 'watershed_calcs')) == 0:
            os.makedirs(os.path.join(self.batch_path, 'watershed_calcs'))
            
        watershed_batch_path = os.path.join(self.batch_path, 'watershed_calcs', timestamp)
        originals_batch_path = os.path.join(watershed_batch_path, 'originals')        
        os.makedirs(watershed_batch_path)
        os.makedirs(originals_batch_path)
        
        # Copy original Pour Points
        shutil.copy2(original_pour_points, originals_batch_path)
        
        working_pp_path = os.path.join(watershed_batch_path, basename(original_pour_points))
        arcpy.CopyFeatures_management(original_pour_points, working_pp_path)
        
        return watershed_batch_path, working_pp_path
            
        
    def pour_points_to_raster(self, pour_points):
        pp_raster_name = self.project_name +'_pp_raster.tif'
        pp_raster_path = os.path.join(self.watershed_batch_path, pp_raster_name)
        arcpy.PointToRaster_conversion(pour_points, "FID", pp_raster_path, 'MOST_FREQUENT', '', '10')        
        
        return pp_raster_path
        
        
    def snap_pour_points(self, pour_points, flow_acc):
        snap_distance = self.pour_points['snap_distance']
        
        out_pp_name = self.project_name + '_snap_ppoints.tif'
        out_pp_path = os.path.join(self.watershed_batch_path, out_pp_name)        
        pp = SnapPourPoint(pour_points, flow_acc, snap_distance, "FID")
        pp.save(out_pp_path)
        
        return out_pp_path
    
            
    def watersheds(self, flow_path, pp_path):
        inPourPointField = "VALUE"
        
        out_ws_name = self.project_name + '_watersheds.tif'
        out_ws_path = os.path.join(self.watershed_batch_path, out_ws_name)
        outWatershed = Watershed(flow_path, pp_path, inPourPointField)
        outWatershed.save(out_ws_path)
        
        return out_ws_path
        
        
    def ws_to_poly(self, ws_path):
        
        out_poly_name = self.project_name + '_poly_ws'
        out_poly_path = os.path.join(self.watershed_batch_path, out_poly_name)
        arcpy.RasterToPolygon_conversion(ws_path, out_poly_path, "NO_SIMPLIFY", 'VALUE')      
        
        return out_poly_path
        
    
    # BQART stuff
    
        
    def climate_batch_directory(self, watershed_directory):
        timestamp = self.get_time_string()
        print('Creating batch files')
        climate_batch_path = os.path.join(watershed_directory, 'climate_calcs', str(timestamp))
        
        os.makedirs(climate_batch_path)
        
        climate_cache_batch_path = os.path.join(watershed_directory, 'climate_cache')
        if not os.path.isdir(climate_cache_batch_path):
            os.makedirs(climate_cache_batch_path)
        
        return climate_batch_path

    def check_climate_cache(self, watershed_directory, climate_scenario, datatype):
        print('Checking for preexisting climate rasters')
        raster_name = datatype + '_' + climate_scenario + '_clip.tif'
        raster_cache_path = os.path.join(watershed_directory, 'climate_cache', raster_name)
        output = False        
        if os.path.isfile(raster_cache_path):
            output = raster_cache_path
        
        return output

    def average_rasters(self, search_directory, save_directory, name, monthly):
        os.chdir(search_directory)
        rasters = []
        for file in glob.glob("*.tif"):
            rasters.append(Raster(os.path.join(search_directory,file)))
         
        raster_sum = sum(rasters)
        
        if monthly: # Temp
            n_rasters = len(rasters)
            combined_raster = raster_sum / n_rasters
        else: # precip
            combined_raster = raster_sum

        combined_raster_path = os.path.join(save_directory, name)
        combined_raster.save(combined_raster_path)
        
        return combined_raster_path


    def clip_raster(self, input_raster, save_directory, name, extent):
        clip_raster_path = os.path.join(save_directory, name)
        arcpy.Clip_management(input_raster, '#', clip_raster_path, extent)    
        
        return clip_raster_path
    

    def zone_statistics(self, table_directory, watersheds, value_raster, data_name):
        table_path = os.path.join(table_directory, data_name)
        outdata = ZonalStatisticsAsTable(watersheds, "VALUE", value_raster, table_path, "DATA")
        
        return outdata      
    
        
    def do_bqart(self, pz_data, tz_data, ez_data, fault_data_path, fault_meta_data, uplift_rate):
        
        t_cursor = arcpy.SearchCursor(tz_data)
        p_cursor = arcpy.SearchCursor(pz_data)
        e_cursor = arcpy.SearchCursor(ez_data)
        
        temps = {}
        precips = {}
        max_reliefs = {}
        min_reliefs = {}
        areas = {}
        
        for row in t_cursor:
            # Get mean temperature
            temps.update({row.getValue('VALUE'): row.getValue('MEAN')})
        
        for row in p_cursor:
            # Get mean precipitation
            precips.update({row.getValue('VALUE'): row.getValue('MEAN')})
            
        for row in e_cursor:
            # Get highest, lowest elevation & area of waters====heds
            max_reliefs.update({row.getValue('VALUE'): row.getValue('MAX')})
            min_reliefs.update({row.getValue('VALUE'): row.getValue('MIN')})
            areas.update({row.getValue('VALUE'): row.getValue('AREA')})
        
        fault_data_output = {}
        
        print('Adding fault data from')
        print(fault_data_path)
        if fault_data_path:
            if os.path.exists(fault_data_path):
                fault_data_output = {}
                with open(fault_data_path, 'rb') as csvfile:
                    fault_data = csv.reader(csvfile, delimiter=',')
                    for row in fault_data:
                        fault_data_output.update({row[0]: [row[1], row[2]]})
        # Unlock data

        
        del row
        del t_cursor
        del p_cursor
        del e_cursor
        
        # BQART
        
        qs_rows = []

        
        # Units!!

        # Qs (kg/s)
        # Qs (m^3/s)
        # A (km^2)
        # R (km)
        # T (C)
        
        # precips are in mm
        # temps are in C x 10
        # relief is in m
        # area is m^2
        
        
        for k in precips.keys():
 
            precip = precips[k] # mm/yr - yearly average
            area_m_squared = areas[k] # m^2
            relief = max_reliefs[k] - min_reliefs[k] # m
            temp = temps[k]/10 # C - yearly average (Worldclim temps need to be divided by 10)
            density = 2700
            omega = 0.0006
            B = 1
            
            if k == 1:
                print({
                    'precip':precip,
                    'area': area_m_squared,
                    'relief':relief,
                    'temp':temp
                })
            
            # Convert precipitation to m/yr
            precip_m = precip / float(1000)
            
            # Relief to km
            relief_km = relief / float(1000)
            
            # Convert area to m^2
            area_km_squared = area_m_squared / float(1000000)
            
            # Precipition to m^3/yr
            precip_m3_yr = precip_m * float(area_m_squared)
            
            # Disharge m^3/s
            Qw_s = precip_m3_yr / float((60*60*24*365))
            
            # Discharge km^3/yr
            Qw_km_yr = math.pow(((Qw_s*31536000)/1000000000),0.31)
            
            # Area 
            A = math.pow(area_km_squared, 0.5)
            
            # Qs in megatons per year
            Qs_MT_yr = omega * B * Qw_km_yr * A * relief_km * temp
            
            # Qs m^3/yr
            Qs_m3_yr = Qs_MT_yr*((1000000000/density)*1.3)           

            Qs_m_yr = Qs_m3_yr / float(area_m_squared)
            
            Qs_mm_yr = Qs_m_yr * float(1000)            
            
            qs = [k, precips[k], omega, B, Qw_s, Qw_km_yr, A, relief_km, temp, Qs_MT_yr, density, Qs_m3_yr, Qs_m_yr, Qs_mm_yr]
            if k == 1:
                print(qs)
            
            if fault_data_output:
                if fault_data_output[str(k)]:
                    
                    fault_id = fault_data_output[str(k)][0]
                    # We're using max
                    fault_slip_mm_yr = fault_meta_data[int(fault_id)]['slip_max']
                    qs.append(fault_slip_mm_yr)
                    
                    # fault slip (m)
                    # corrected for density
                    Q_tectonic = (area_m_squared * (fault_slip_mm_yr/float(1000)))/float(density)
                    
                    # Simple Qs
                    qs.append(Q_tectonic)                   
                    
                    # Fault number
                    qs.append(fault_id)
                    # Fault name
                    qs.append(fault_meta_data[int(fault_id)]['name'])
                    # Distance
                    qs.append(fault_data_output[str(k)][1])
            else:
                Uplift_mm_yr = uplift_rate
                Uplift_metres_yr = Uplift_mm_yr / float(1000)
                Q_tectonic = Area_square_m * Uplift_metres_yr
                qs.append(Uplift_mm_yr)
                qs.append(Q_tectonic)
                
            qs_rows.append(qs)
            
        return qs_rows
    
    def save_data_to_csv(self, qs_data, path):
        data_name = 'qs_data.csv'
        row_headers = ['id', 'precipitation (mm/yr)', 'w', 'B', 'Qw (m^3/s)', 'Qw (km^3/yr)', 'A (km^2)', 
                       'R (km)', 'T(C)', 'Qs (MT/y)', 'density (kg/m^3)', 'Qs (m^3/yr)', 
                       'erosion (m/yr)', 'erosion (mm/yr)', 'Slip (mm/yr)', 'Qs Tectonic (m^3/yr)']
                       
        if len(qs_data[0]) > 16:
            row_headers.append('fault id')
            row_headers.append('fault name')
            row_headers.append('distance')
        
        data_path = os.path.join(path, data_name)
        with open(data_path, 'wb') as qs_file:
            a = csv.writer(qs_file, delimiter=',')
            a.writerow(row_headers)
            for r in qs_data:
                a.writerow(r)
         
        print('Data saved to '+data_path)
    
def save_last_run(root, key, val):
    
    last_run_path = os.path.join(root, 'last_run.yml')
    
    if os.path.isfile(last_run_path):
        f = open(last_run_path)
        last_run = yaml.load(f.read())
        f.close()
    else:
        last_run = {}
        
    last_run.update({key: val})
    
    with open(last_run_path, 'w') as outfile:
        outfile.write(yaml.dump(last_run, default_flow_style=True))

        
def load_last_run(root):
    # Check if last run exists
    last_run = False

    if os.path.exists(os.path.join(root, 'last_run.yml')):
        f = open(os.path.join(root, 'last_run.yml'))
        last_run = yaml.load(f.read())
        f.close()
    
    return last_run

def select_batch_directory(root_dir):
    os.chdir(root_dir)
    times = {}
    days = {}
    
    for dir_name in glob.glob("*"):
        t_frags = dir_name.split('_')
        t_int = map(int, t_frags)
        dt = datetime.datetime(t_int[0], t_int[1], t_int[2], t_int[3], t_int[4], t_int[5])
        times.update({dir_name: dt})
        ds = '_'.join([t_frags[0], t_frags[1], t_frags[2]])
        days.update({ds:{}})
                
    for k in times.keys():
        t_frags = k.split('_')
        ds = '_'.join([t_frags[0], t_frags[1], t_frags[2]])
        t_string = times[k].strftime("%H:%M:%S")
        days[ds].update({t_string:k})
    
    d_strings = []
    d_frag_list = []
    
    # Ask which days
    for k in days:
        d_frags = k.split('_')
        d_int = map(int, d_frags)
        dt = datetime.datetime(d_int[0], d_int[1], d_int[2])
        d_strings.append(dt.strftime("%a %b %d %Y"))
        d_frag_list.append(k)
    
    
    p1 = shell.Prompt("Pick batch day", options = d_strings, numbered = True)
    
    # Ask which times
    d_index = d_strings.index(p1.input)
    d_choice = days[d_frag_list[d_index]]
    
    p2 = shell.Prompt("Pick batch time", options = d_choice.keys(), numbered = True)   
    
    choice = d_choice[p2.input]
    
    return choice

         
try:
    app.setup()
    
    app.run()
    
    if app.pargs.config:
        try:
            f = open(app.pargs.config)
            yaml_config = yaml.load(f.read())
            f.close()
            
            last_settings = load_last_run(yaml_config['root'])
            hydro_batch = False
            watershed_batch = False
            
            
            if last_settings:
                p = shell.Prompt("Use last use settings?", ['y','n'])
                if p.input is 'y':
                    if 'hydro_batch' in last_settings:
                        hydro_batch = last_settings['hydro_batch']
                      
                    if 'watershed_batch' in last_settings:
                        watershed_batch = last_settings['watershed_batch']          
            
            if app.skip_to_discharge == 0:
                if app.skip_to_watersheds == 0:
                    gbatch = GISbatch(yaml_config)
                    hydro_paths = gbatch.hydro_workflow()
                    save_last_run(yaml_config['root'], 'hydro_batch', os.path.dirname(os.path.realpath(hydro_file_path)))
                else:
                    try:
                        
                        if hydro_batch:
                            # Using last used hydro_batch
                            gbatch = GISbatch(yaml_config, hydro_batch)
                            hydro_file_path = os.path.join(hydro_batch, 'hydro_paths.yml')
                        else:
                            print 'Pick hydro path batch'
                            tchoice = select_batch_directory(os.path.join(yaml_config['root'], 'Output'))
                            batch = os.path.join(yaml_config['root'], 'Output', tchoice)
                            gbatch = GISbatch(yaml_config, batch)
                            hydro_file_path = os.path.join(gbatch.batch_path, 'hydro_paths.yml')
                            
                        if os.path.exists(hydro_file_path):
                            f = open(hydro_file_path)
                            hydro_paths = yaml.load(f.read())
                            f.close()
                        else:
                            print('Cannot find '+ hydro_file_path)
                            hydro_paths_exists = 0
                            while hydro_paths_exists == 0:
                                p = shell.Prompt("Path to hydro_paths config file: ")
                                if os.path.exists(p.input):
                                    f = open(p.input)
                                    hydro_paths = yaml.load(f.read())
                                    f.close() 
                                    hydro_paths_exists = 1
                                else:
                                    print('File does not exist!')
                                    
                        gbatch.save_last_run(yaml_config['root'], 'hydro_batch', os.path.dirname(os.path.realpath(hydro_file_path)))
                        
                    except (OSError, IOError) as e:
                        print(e)
                        exit

                if os.path.exists(gbatch.pour_points_path):
                    pour_point_path = gbatch.pour_points_path
                else:
                    pour_point_path = 0
                    process_faults = 0
                    
                    if gbatch.fault_path:
                        if os.path.exists(gbatch.fault_path):
                            process_faults = 1
                        
                    if process_faults:
                        pour_point_path = gbatch.fault_workflow(gbatch.fault_path, hydro_paths)
                    else:
                        while pour_point_path == 0:
                            p = shell.Prompt("Path to pour point shapefile: ")
                            if os.path.exists(p.input):
                                pour_point_path = p.input
                            else:
                                print('File does not exist!')

                watershed_raster = gbatch.watershed_workflow(pour_point_path, hydro_paths)
                watershed_directory = os.path.dirname(os.path.realpath(watershed_raster))
                
            else: # Skip to discharge calculations

                hydro_paths = 0
                watershed_raster = 0
                
                if hydro_batch:
                    # Using last used hydro_batch
                    gbatch = GISbatch(yaml_config, hydro_batch)
                    hydro_file_path = os.path.join(hydro_batch, 'hydro_paths.yml')
                else:
                    print 'Pick hydro path batch'
                    tchoice = select_batch_directory(os.path.join(yaml_config['root'], 'Output'))
                    batch = os.path.join(yaml_config['root'], 'Output', tchoice)
                    gbatch = GISbatch(yaml_config, batch)
                    hydro_file_path = os.path.join(gbatch.batch_path, 'hydro_paths.yml')
                    
                if os.path.exists(hydro_file_path):
                    f = open(hydro_file_path)
                    hydro_paths = yaml.load(f.read())
                    f.close()
                else:
                    print('Cannot find '+ hydro_file_path)
                    hydro_paths_exists = 0
                    while hydro_paths_exists == 0:
                        p = shell.Prompt("Path to hydro_paths config file: ")
                        if os.path.exists(p.input):
                            f = open(p.input)
                            hydro_paths = yaml.load(f.read())
                            f.close() 
                            hydro_paths_exists = 1
                        else:
                            print('File does not exist!')
                            
                h_dir = os.path.dirname(os.path.realpath(hydro_file_path))
                save_last_run(yaml_config['root'], 'hydro_batch', h_dir)
                
                if watershed_batch:
                     watershed_directory = watershed_batch
                     watershed_raster = os.path.join(watershed_directory, 
                                gbatch.project_name + '_watersheds.tif')    
                else:
                    print 'Pick watershed path batch'
                    watershed_calcs = os.path.join(h_dir, 'watershed_calcs')
                    
                    tchoice = select_batch_directory(watershed_calcs)
                    
                    watershed_raster = os.path.join(watershed_calcs, tchoice, 
                                gbatch.project_name + '_watersheds.tif')
                    
                    watershed_directory = os.path.dirname(os.path.realpath(watershed_raster))
                    save_last_run(yaml_config['root'], 'watershed_batch', watershed_directory)

            # Pick climate scenario
            climate_by_name = {}
            climate_names = []
            for c in gbatch.climates:
                climate_by_name.update({c['name']: c})
                climate_names.append(c['name'])
            
            p = shell.Prompt("Pick climate scenario", options = climate_names, numbered = True)

            climate_scenario = climate_by_name[p.input]
            temperature_directory = climate_scenario['temp_directory']
            precipitation_directory = climate_scenario['precip_directory']
            
            gbatch.bqart_workflow(watershed_raster, hydro_paths, 
                                  watershed_directory, temperature_directory, 
                                  precipitation_directory, p.input)
            
        except (OSError, IOError) as e:
            print(e)
            exit
    else:
        print('Please define path to config file -c CONFIG')
    
finally:
    arcpy.CheckInExtension("Spatial")
    app.close()
 


        
        