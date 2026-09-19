#!/usr/bin/env python3
"""
CSV Data Anonymizer

This script takes a CSV file as input, removes PII columns, transforms timestamps to hours,
and outputs an anonymized CSV file.

Usage:
    python data_transformator.py input.csv output.csv
"""

import numpy as np
import pandas as pd
import sys
import argparse
from datetime import datetime

# Define PII columns to be removed
PII_COLUMNS = ['msisdn', 'imsi']

def pseudonymize_value_imei(imei_value: int) -> int:
    """
    Pseudonymize IMEI value by hashing or masking.
    Args:
        value: Original IMEI value
    Returns:
        Pseudonymized IMEI value (first 8 digits correlating with device type and model)
    """

    if not imei_value or imei_value == '':
        return None

    try:
        imei_pseudonymized = imei_value // 10000000  #% 100000000  # Keep first 8 digits

    except (ValueError, TypeError):
        imei_pseudonymized = None

    return imei_pseudonymized

def transform_timestamp_to_hours(timestamp: int) -> int:
    """
    Transform timestamp to hours since epoch.

    Args:
        timestamp_str: Timestamp string in format YYMMDDHHMM (e.g., 1809100800)

    Returns:
        Integer representing hours since epoch
    """
    if not timestamp or timestamp == '':
        return None

    try:
        # Create datetime object
        dt = datetime.fromtimestamp(timestamp)

        # Calculate hours since epoch (Unix timestamp / 3600)
        epoch = datetime(1970, 1, 1)
        hours_since_epoch = int((dt - epoch).total_seconds() / 3600)

        return hours_since_epoch
    except (ValueError, TypeError):
        # If transformation fails, return original value
        return timestamp

def anonymize_csv(input_file, output_file):
    """
    Anonymize CSV data by removing PII columns and transforming timestamps.

    Args:
        input_file: Path to input CSV file
        output_file: Path to output CSV file
    """
    df = pd.read_csv(input_file)
    df.drop(columns=PII_COLUMNS, inplace=True, errors='ignore')  # Remove PII columns if they exist
    df['imei'] = df['imei'].apply(pseudonymize_value_imei)  # Pseudonymize IMEI
    df['time_start'] = df['time_start'].apply(transform_timestamp_to_hours)  # Transform timestamps
    df.to_csv(output_file, index=False)

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