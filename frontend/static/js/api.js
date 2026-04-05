// frontend/static/js/api.js

const API_BASE_URL = window.location.origin; // Dynamically uses localhost:8000 or your production domain

const API = {
    /**
     * Fetches all campaign data for the dashboard.
     */
    async getDashboardData() {
        try {
            const response = await fetch(`${API_BASE_URL}/api/dashboard`);
            if (!response.ok) throw new Error("Failed to fetch dashboard data");
            return await response.json();
        } catch (error) {
            console.error("[API Error] getDashboardData:", error);
            throw error;
        }
    },

    /**
     * Sends the JD and Resume to prepare the interview and send the email.
     */
    async createCampaign(formData) {
        try {
            const response = await fetch(`${API_BASE_URL}/prepare-interview/`, {
                method: 'POST',
                body: formData // FormData automatically sets the correct multipart headers
            });
            const result = await response.json();
            if (!response.ok) throw new Error(result.detail || "Failed to create campaign");
            return result;
        } catch (error) {
            console.error("[API Error] createCampaign:", error);
            throw error;
        }
    },

    /**
     * Fetches the detailed Mistral evaluation report for a candidate.
     */
    async getReport(sessionId) {
        try {
            const response = await fetch(`${API_BASE_URL}/api/interview-session/${sessionId}`);
            const result = await response.json();
            if (result.status !== 'success') throw new Error("Report not found or not completed");
            return result.data;
        } catch (error) {
            console.error("[API Error] getReport:", error);
            throw error;
        }
    }
};

// Export to global window object so HTML scripts can use it
window.API = API;
