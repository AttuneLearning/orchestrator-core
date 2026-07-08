"""Tests for contract acceptance error handling."""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

from .app import create_app
from ..db import get_pool


def test_contract_accept_no_proposal():
    """Test that accepting a contract with no pending proposal returns a 400 error."""
    # Create a mock pool
    mock_pool = MagicMock()
    
    # Mock the repository to raise ValueError when no proposal exists
    with patch('orchestrator.repository.accept_proposal') as mock_accept_proposal:
        mock_accept_proposal.side_effect = ValueError("no pending proposal for GET /clients/:id/notes")
        
        # Create app with mock pool
        app = create_app(pool=mock_pool)
        client = TestClient(app)
        
        # Test the endpoint
        response = client.post("/contracts/accept", data={
            "method": "GET",
            "path": "/clients/:id/notes"
        })
        
        # Should return 400 with error message
        assert response.status_code == 400
        assert "Contract Acceptance Error" in response.text
        assert "no pending proposal for GET /clients/:id/notes" in response.text


def test_contract_accept_with_issue_no_proposal():
    """Test that accepting a contract with issue creation and no pending proposal returns a 400 error."""
    # Create a mock pool
    mock_pool = MagicMock()
    
    # Mock the repository to raise ValueError when no proposal exists
    with patch('orchestrator.repository.get_proposal') as mock_get_proposal:
        mock_get_proposal.side_effect = ValueError("no pending proposal for GET /clients/:id/notes")
        
        # Create app with mock pool
        app = create_app(pool=mock_pool)
        client = TestClient(app)
        
        # Test the endpoint
        response = client.post("/contracts/accept_with_issue", data={
            "method": "GET",
            "path": "/clients/:id/notes"
        })
        
        # Should return 400 with error message
        assert response.status_code == 400
        assert "Contract Acceptance Error" in response.text
        assert "no pending proposal for GET /clients/:id/notes" in response.text


def test_contract_accept_removal_no_proposal():
    """Test that accepting a contract removal with no pending proposal returns a 400 error."""
    # Create a mock pool
    mock_pool = MagicMock()
    
    # Mock the repository to raise ValueError when no proposal exists
    with patch('orchestrator.repository.get_proposal') as mock_get_proposal:
        mock_get_proposal.side_effect = ValueError("no pending proposal for GET /clients/:id/notes")
        
        # Create app with mock pool
        app = create_app(pool=mock_pool)
        client = TestClient(app)
        
        # Test the endpoint
        response = client.post("/contracts/accept_removal", data={
            "method": "GET",
            "path": "/clients/:id/notes"
        })
        
        # Should return 400 with error message
        assert response.status_code == 400
        assert "Contract Acceptance Error" in response.text
        assert "no pending proposal for GET /clients/:id/notes" in response.text


def test_contract_mark_redevelopment_no_proposal():
    """Test that marking a contract for redevelopment with no pending proposal returns a 400 error."""
    # Create a mock pool
    mock_pool = MagicMock()
    
    # Mock the repository to raise ValueError when no proposal exists
    with patch('orchestrator.repository.get_proposal') as mock_get_proposal:
        mock_get_proposal.side_effect = ValueError("no pending proposal for GET /clients/:id/notes")
        
        # Create app with mock pool
        app = create_app(pool=mock_pool)
        client = TestClient(app)
        
        # Test the endpoint
        response = client.post("/contracts/mark_redevelopment", data={
            "method": "GET",
            "path": "/clients/:id/notes"
        })
        
        # Should return 400 with error message
        assert response.status_code == 400
        assert "Contract Acceptance Error" in response.text
        assert "no pending proposal for GET /clients/:id/notes" in response.text