# Foundify: Smart Campus Lost & Found System

Foundify is an intelligent, automated Lost & Found platform designed specifically for college campuses and large communities. It leverages AI-based image matching (using Computer Vision) to automatically pair reported lost items with found items, significantly reducing the manual effort of recovering lost valuables.

## Key Features

*   **AI-Powered Image Matching**: Uses OpenCV and Spacy for NLP to cross-reference images and descriptions, providing a confidence score for potential matches.
*   **Distinct User Roles**:
    *   **Users**: Can report lost/found items, chat securely with matched users, and view personalized dashboards with real-time statistics and community leaderboards.
    *   **Admins / Super Admins**: Have access to an "Admin Desk" to verify AI matches, manage users, configure system settings, and generate PDF executive reports.
*   **Modern Premium UI**: Fully responsive, high-end design using TailwindCSS, featuring glassmorphism, animated loaders, and custom component styling.
*   **Real-time Chat**: Integrated `flask-socketio` for live, secure messaging between users who have successfully matched items.
*   **Secure Authentication**: Includes Google OAuth integration, standard email/password login, and password recovery via secure email links.

##  Technology Stack

*   **Backend**: Python, Flask, Flask-SocketIO
*   **Database**: MongoDB (Atlas) + GridFS for image storage
*   **Frontend**: HTML5, TailwindCSS (via CDN), JavaScript, Google Material Icons
*   **AI/ML**: OpenCV (image processing), SpaCy (text matching), Pillow-HEIF
*   **Deployment**: Ready for Render/Heroku (Gunicorn, Eventlet)

##  Local Setup Instructions

1.  **Clone the Repository**
    ```bash
    git clone https://github.com/rikshith26/devsquad-pu239.git
    cd devsquad-pu239
    ```

2.  **Set up a Virtual Environment**
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```

3.  **Install Dependencies**
    ```bash
    pip install -r requirements.txt
    
    # Download the required spaCy model for text matching
    python -m spacy download en_core_web_md
    ```

4.  **Environment Variables**
    Create a `.env` file in the root directory and add your configurations:
    ```env
    MONGO_URI=your_mongodb_connection_string
    DB_NAME=lost_found_ai
    MAIL_USERNAME=your_email@example.com
    MAIL_PASSWORD=your_app_password
    GOOGLE_CLIENT_ID=your_google_oauth_client_id
    GOOGLE_CLIENT_SECRET=your_google_oauth_client_secret
    ```

5.  **Run the Application**
    ```bash
    python app.py
    ```
    The application will start with SocketIO support on `http://127.0.0.1:5000`.

##  Deployment

The application is fully configured for cloud deployment on platforms like **Render** or **Heroku**:
*   A `Procfile` is included (`web: gunicorn --worker-class eventlet -w 1 app:app`).
*   `app.py` dynamically binds to the port provided by the host environment.
*   Ensure all `.env` variables are added to your hosting provider's Environment Secrets panel.

##  Contributing
This project is built by DevSquad. Contributions, issues, and feature requests are welcome!
