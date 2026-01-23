#!/bin/bash

# Check for Python3
if ! command -v python3 &> /dev/null; then
    echo "Python3 is not installed. Please install Python3."
    exit 1
fi

# Check for virtual environment
if [ ! -d ".venv" ]; then
    echo "No virtual environment found. Creating environment..."
    
    # create virtual environment
    python3 -m venv .venv
    
    # activate virtual environment
    source .venv/bin/activate
    
    # Install requirements.txt
    if [ -f "requirements.txt" ]; then
        echo "Installing requirements..."
        pip install -r requirements.txt
    else
        echo "No requirements.txt found."
    fi
else
    echo "Found virtual environment. Activating..."
    
    # activate virtual environment
    source .venv/bin/activate
fi

# Start app with Python3
echo "Starting App..."
python3 main.py
