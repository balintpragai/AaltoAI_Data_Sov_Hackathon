#!/usr/bin/env python3
"""
CSV Data Anonymizer

This script takes a CSV file as input, removes PII columns, transforms timestamps to hours,
and outputs an anonymized CSV file.

Usage:
    python data_transformator.py input.csv output.csv
"""

import csv
import sys
import argparse
from datetime import datetime

# Define PII columns to be removed
PII_COLUMNS = ['msisdn', 'imsi', 'imei']

def transform_timestamp_to_hours(timestamp_str):
    """
    Transform timestamp to hours since epoch.

    Args:
        timestamp_str: Timestamp string in format YYMMDDHHMM (e.g., 1809100800)

    Returns:
        Integer representing hours since epoch
    """
    if not timestamp_str or timestamp_str == '':
        return None

    try:
        # Parse the timestamp string
        timestamp = int(timestamp_str)
        # Extract components: YYMMDDHHMM
        year = 2000 + (timestamp // 100000000)
        month = (timestamp // 1000000) % 100
        day = (timestamp // 10000) % 100
        hour = (timestamp // 100) % 100
        minute = timestamp % 100

        # Create datetime object
        dt = datetime(year, month, day, hour, minute)

        # Calculate hours since epoch (Unix timestamp / 3600)
        epoch = datetime(1970, 1, 1)
        hours_since_epoch = int((dt - epoch).total_seconds() / 3600)

        return hours_since_epoch
    except (ValueError, TypeError):
        # If transformation fails, return original value
        return timestamp_str

def anonymize_csv(input_file, output_file):
    """
    Anonymize CSV data by removing PII columns and transforming timestamps.

    Args:
        input_file: Path to input CSV file
        output_file: Path to output CSV file
    """
    with open(input_file, mode='r', newline='', encoding='utf-8') as infile, \
         open(output_file, mode='w', newline='', encoding='utf-8') as outfile:

        reader = csv.DictReader(infile)
        fieldnames = reader.fieldnames

        # Filter out PII columns
        filtered_fieldnames = [field for field in fieldnames if field not in PII_COLUMNS]

        writer = csv.DictWriter(outfile, fieldnames=filtered_fieldnames)
        writer.writeheader()

        for row in reader:
            # Create new row with PII columns removed
            new_row = {field: row[field] for field in filtered_fieldnames}

            # Transform timestamp if present
            if 'time_start' in new_row:
                new_row['time_start'] = transform_timestamp_to_hours(new_row['time_start'])

            writer.writerow(new_row)

def main():
    """Main function to handle command line arguments and execute anonymization."""
    parser = argparse.ArgumentParser(description='Anonymize CSV data by removing PII columns and transforming timestamps.')
    parser.add_argument('input_file', help='Path to input CSV file')
    parser.add_argument('output_file', help='Path to output CSV file')
    args = parser.parse_args()

    try:
        anonymize_csv(args.input_file, args.output_file)
        print(f"Successfully anonymized data. Output saved to {args.output_file}")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()